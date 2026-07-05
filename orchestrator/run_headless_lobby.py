import os
import sys
import json
import time
import subprocess
import argparse
import fnmatch

# Structured JSONL logging (see docs/features/doing/structured-logging.md). Imported
# defensively: if event_log.py isn't co-deployed (e.g. a partial rollout that only pushed
# run_headless_lobby.py), the orchestrator must still run -- logging degrades to a no-op
# rather than crashing the control loop.
try:
    from event_log import EventLogger, resolve_log_dir
    _EVENT_LOG_AVAILABLE = True
except Exception as _event_log_import_err:  # pragma: no cover - defensive
    _EVENT_LOG_AVAILABLE = False
    print(f"[Host] WARNING: event_log module unavailable ({_event_log_import_err}); "
          f"structured logging disabled.")


class _NullLogger:
    """No-op stand-in with the EventLogger method surface, used when event_log is
    unavailable so call sites need no `if logger` guards for availability."""

    def _noop(self, *args, **kwargs):
        return None

    def __getattr__(self, _name):
        return self._noop


def make_event_logger(config, project_dir):
    """Build the structured event logger (or a no-op stand-in if the module is missing)."""
    if not _EVENT_LOG_AVAILABLE:
        return _NullLogger()
    try:
        log_dir = resolve_log_dir(config, project_dir)
        return EventLogger(log_dir)
    except Exception as e:  # pragma: no cover - defensive
        print(f"[Host] WARNING: failed to initialize structured logging ({e}); disabling.")
        return _NullLogger()

def load_track_mode_availability(plugins_dir):
    """Ground-truth (environment, game mode) -> [track names] data, produced by the plugin's
    nested dropdown dump (BepInEx/plugins/track_mode_availability.json). Returns None if the
    file is missing/unreadable so callers can fail open rather than block on stale/missing data.
    """
    path = os.path.join(plugins_dir, "track_mode_availability.json")
    if not os.path.exists(path):
        print("[Playlist] WARNING: track_mode_availability.json not found — skipping mode/availability cross-validation.")
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Playlist] WARNING: Failed to load track_mode_availability.json: {e}")
        return None


def cross_validate_tracks(resolved_tracks, availability_data, env_normalization):
    """Drops (track, env, mode) entries that the live game doesn't actually offer. Fails open
    (keeps the entry) whenever there's no ground-truth data to check against, so a missing or
    partial dump can never itself cause a hang.
    """
    if availability_data is None:
        return resolved_tracks, 0, 0

    kept = []
    dropped_missing = 0
    dropped_mode = 0
    for track_name, env_key, game_mode in resolved_tracks:
        env_norm = env_normalization.get(env_key.lower().strip(), env_key)
        env_modes = availability_data.get(env_norm)
        if env_modes is None:
            kept.append((track_name, env_key, game_mode))
            continue

        mode_tracks = env_modes.get(game_mode)
        if mode_tracks is None:
            kept.append((track_name, env_key, game_mode))
            continue

        track_norm = track_name.lower().strip()
        if any(t.lower().strip() == track_norm for t in mode_tracks):
            kept.append((track_name, env_key, game_mode))
            continue

        in_any_mode = any(
            track_norm in (t.lower().strip() for t in tlist)
            for tlist in env_modes.values()
        )
        if in_any_mode:
            dropped_mode += 1
            print(f"[Playlist] DROP '{track_name}' ({env_key}, {game_mode}): mode_unsupported")
        else:
            dropped_missing += 1
            print(f"[Playlist] DROP '{track_name}' ({env_key}, {game_mode}): not_installed_or_not_shared")

    return kept, dropped_missing, dropped_mode


def round_robin_shuffle_by_environment(resolved_tracks):
    """Shuffles each environment's tracks among themselves, shuffles the order environments
    are visited in, then interleaves round-robin across environments. A plain flat shuffle
    can (and does, by chance) land two tracks from the same environment back-to-back — the
    environment is the dominant visual cue players notice, so that reads as "stale"/repetitive
    even though no individual track repeated. Round-robin guarantees same-environment picks
    are spread apart (until an environment runs out of tracks) while staying genuinely random.
    """
    import random

    groups = {}
    env_order = []
    for track in resolved_tracks:
        env = track[1]
        if env not in groups:
            groups[env] = []
            env_order.append(env)
        groups[env].append(track)

    for env in env_order:
        random.shuffle(groups[env])
    random.shuffle(env_order)

    result = []
    round_idx = 0
    while len(result) < len(resolved_tracks):
        added_any = False
        for env in env_order:
            if round_idx < len(groups[env]):
                result.append(groups[env][round_idx])
                added_any = True
        if not added_any:
            break
        round_idx += 1
    return result


def resolve_and_write_playlist(playlist_name, shuffle_enabled, output_file, is_fallback=False, logger=None):
    import random

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    playlists_path = os.path.join(project_dir, "playlists.json")
    master_list_path = os.path.join(project_dir, "master_tracks_list.json")
    
    # Check playlists file
    if not os.path.exists(playlists_path):
        print(f"ERROR: Playlists file not found at {playlists_path}")
        return
        
    with open(playlists_path, "r") as f:
        playlists_data = json.load(f)
        
    if playlist_name not in playlists_data:
        raise ValueError(f"Playlist '{playlist_name}' not found in playlists.json. Available: {list(playlists_data.keys())}")
        
    playlist_items = playlists_data[playlist_name]
    
    if not os.path.exists(master_list_path):
        print(f"ERROR: master_tracks_list.json not found at {master_list_path}")
        sys.exit(1)
        
    with open(master_list_path, "r") as f:
        master_data = json.load(f)
        
    resolved_tracks = []
    
    def is_match(pattern, value):
        return fnmatch.fnmatch(value.lower().strip(), pattern.lower().strip())

    # Maps any variant spelling to the canonical display name used as master list keys
    ENV_NORMALIZATION = {
        "thedrawingboard": "The Drawing Board",
        "thedrawingboardcyber": "The Drawing Board",
        "the drawing board": "The Drawing Board",
        "thegreen": "The Green",
        "the green": "The Green",
        "hannover": "Hannover",
        "hall26": "Hall 26",
        "hall 26": "Hall 26",
        "autumn fields": "Autumn Fields",
        "autumnfields": "Autumn Fields",
        "bando city": "Bando City",
        "bandocity": "Bando City",
        "hangar c03": "Hangar C03",
        "hangarc03": "Hangar C03",
        "liftoff arena": "Liftoff Arena",
        "liftoffarena": "Liftoff Arena",
        "pine valley": "Pine Valley",
        "pinevalley": "Pine Valley",
        "straw bale": "Straw Bale",
        "strawbale": "Straw Bale",
        "minus two": "Minus Two",
        "minustwo": "Minus Two",
        "dubai legends": "Dubai Legends",
        "dubailegends": "Dubai Legends",
        "paris drone festival": "Paris Drone Festival",
        "parisdronefestival": "Paris Drone Festival",
        "the pit": "The Pit",
        "thepit": "The Pit",
        "bardwell's yard": "Bardwell's Yard",
        "bardwellsyard": "Bardwell's Yard",
        "russian woodpecker": "The Woodpecker",
        "russianwoodpecker": "The Woodpecker",
        "the woodpecker": "The Woodpecker",
        "thewoodpecker": "The Woodpecker",
        "short circuit": "Short Circuit",
        "shortcircuit": "Short Circuit",
        "marina bay": "Marina Bay",
        "marinabay": "Marina Bay",
        "surtur": "Surtur",
        "permafrost": "Permafrost",
        "rustline": "Rustline",
        "azure district": "Azure District",
        "azuredistrict": "Azure District",
        "melon pan park": "Melon Pan Park",
        "melonpanpark": "Melon Pan Park"
    }

    for item in playlist_items:
        if isinstance(item, str):
            env_pattern = "*"
            track_pattern = item
            game_mode = "Infinite Race"
        elif isinstance(item, dict):
            env_pattern = item.get("environment", "*")
            track_pattern = item.get("track", "*")
            game_mode = item.get("mode", "Infinite Race")
        else:
            continue
        
        # Match environments in master list
        for env_key in master_data:
            # Resolve pattern if specific name
            target_env_key = ENV_NORMALIZATION.get(env_pattern.lower().strip(), env_pattern)
            
            if env_pattern != "*" and target_env_key.lower().strip() != env_key.lower().strip():
                continue
                
            # Environment matches, match tracks in shareable categories only.
            # "local" tracks are intentionally excluded here: they can't be shared to other
            # players (see docs/features/done/race-not-shared-handling.md), and
            # gather_tracks_and_races() never writes anything outside official/workshop/local.
            for category in ["official", "workshop"]:
                if category not in master_data[env_key]:
                    continue
                for track_name in master_data[env_key][category]:
                    if is_match(track_pattern, track_name):
                        track_entry = (track_name, env_key, game_mode)
                        if track_entry not in resolved_tracks:
                            resolved_tracks.append(track_entry)
                        
    print(f"[Playlist] Resolved {len(resolved_tracks)} tracks for playlist '{playlist_name}' from master list.")

    # Cross-validate against the plugin's live-game ground truth (which tracks are actually
    # installed/shared, and which game modes each one really supports) before committing to
    # this rotation. Fails open if the dump isn't available yet.
    plugins_dir = os.path.dirname(os.path.abspath(output_file))
    availability_data = load_track_mode_availability(plugins_dir)
    resolved_tracks, n_missing, n_mode = cross_validate_tracks(resolved_tracks, availability_data, ENV_NORMALIZATION)
    print(f"[Playlist] Cross-validation: kept {len(resolved_tracks)} "
          f"(dropped {n_missing} not_installed_or_not_shared, {n_mode} mode_unsupported)")

    if not resolved_tracks:
        if not is_fallback and playlist_name != "all_official_races" and "all_official_races" in playlists_data:
            print(f"[Playlist] CRITICAL: '{playlist_name}' resolved to 0 valid tracks after cross-validation. "
                  f"Falling back to 'all_official_races'.")
            if logger:
                logger.error("playlist resolved to 0 tracks; falling back to all_official_races",
                             context="playlist_resolution", playlist=playlist_name)
            return resolve_and_write_playlist("all_official_races", shuffle_enabled, output_file,
                                              is_fallback=True, logger=logger)
        else:
            print(f"[Playlist] CRITICAL: '{playlist_name}' resolved to 0 tracks (fallback exhausted or unavailable). "
                  f"Writing empty rotation file — bot may get stuck.")
            if logger:
                logger.error("playlist resolved to 0 tracks; fallback exhausted, writing empty rotation",
                             context="playlist_resolution", playlist=playlist_name)

    # tracks_to_rotate.txt's line order IS the rotation order — there's no separate
    # "shuffled" vs "unshuffled" state. When shuffle is requested we randomize the
    # order once here; the plugin re-shuffles this same file in place (and resets
    # rotation_state.txt to 0) whenever it completes a full pass or shuffle is
    # toggled on mid-session, so every fresh deal is a genuine new permutation.
    if shuffle_enabled:
        resolved_tracks = round_robin_shuffle_by_environment(resolved_tracks)
        print("[Playlist] Shuffling track rotation order (round-robin by environment)...")
    else:
        print("[Playlist] Using track rotation in playlist definition order...")

    # Write to tracks_to_rotate.txt
    with open(output_file, "w") as f:
        f.write("# Generated from playlist: " + playlist_name + "\n")
        for track_name, ui_env, game_mode in resolved_tracks:
            f.write(f"{track_name},{ui_env},{game_mode}\n")
    print(f"[Playlist] Wrote tracks to rotate to: {output_file}")

    # The orchestrator's "track change" signal: the set of tracks in rotation just changed.
    if logger:
        logger.playlist_resolved(playlist_name, len(resolved_tracks), shuffle=shuffle_enabled,
                                 dropped_missing=n_missing, dropped_mode=n_mode,
                                 fallback=is_fallback)

    # Reset rotation state to 0 — the single cursor used for both modes.
    state_file = os.path.join(os.path.dirname(output_file), "rotation_state.txt")
    try:
        with open(state_file, "w") as f:
            f.write("0")
        print(f"[Playlist] Reset rotation state in: {state_file}")
    except Exception as e:
        print(f"[Playlist] WARNING: Failed to reset rotation state file: {e}")


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lobby_config.json")
    if not os.path.exists(config_path):
        print(f"ERROR: Configuration file not found at {config_path}")
        sys.exit(1)
    with open(config_path, "r") as f:
        return json.load(f)

def run_command(cmd, env=None, check=True):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=check)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {' '.join(cmd)}\nError: {e.stderr}")
        raise

def get_active_liftoff_pids():
    pids = []
    try:
        import getpass
        current_user = getpass.getuser()
        out = run_command(["pgrep", "-u", current_user, "-x", "Liftoff.x86_64"], check=False)
        for line in out.splitlines():
            pid = line.strip()
            if pid.isdigit():
                pids.append(pid)
    except Exception:
        pass
    return pids

def get_steam_dbus_address():
    try:
        import getpass
        current_user = getpass.getuser()
        import subprocess
        out = subprocess.run(["pgrep", "-u", current_user, "-x", "steam"], stdout=subprocess.PIPE, text=True)
        pids = [p.strip() for p in out.stdout.splitlines() if p.strip().isdigit()]
        for pid in pids:
            environ_path = f"/proc/{pid}/environ"
            if os.path.exists(environ_path):
                with open(environ_path, "rb") as f:
                    env_data = f.read()
                parts = env_data.split(b"\x00")
                for part in parts:
                    if part.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                        return part.decode("utf-8", errors="ignore").split("=", 1)[1]
    except Exception as e:
        print(f"[Host] Warning: failed to extract Steam DBUS address: {e}")
    return None

def main():
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Sanitize environment variables if they belong to another user to prevent Steam IPC hijacking
    try:
        import getpass
        import pwd
        current_user = getpass.getuser()
        
        # Check XDG_RUNTIME_DIR
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if xdg_runtime_dir:
            try:
                stat_info = os.stat(xdg_runtime_dir)
                owner_uid = stat_info.st_uid
                owner_name = pwd.getpwuid(owner_uid).pw_name
                if owner_name != current_user:
                    print(f"[Host] Warning: XDG_RUNTIME_DIR ({xdg_runtime_dir}) belongs to user '{owner_name}', but we are running as '{current_user}'.")
                    print("[Host] Automatically clearing XDG_RUNTIME_DIR to prevent Steam IPC redirection/hijacking.")
                    del os.environ["XDG_RUNTIME_DIR"]
            except Exception:
                pass
                
        # Clear DBUS session bus address if running as bot user to prevent session contamination
        if current_user == "fpv_bot" and "DBUS_SESSION_BUS_ADDRESS" in os.environ:
            del os.environ["DBUS_SESSION_BUS_ADDRESS"]
            
        if current_user != "fpv_bot":
            print(f"[Host] NOTE: Running as user '{current_user}'. If you intended to start the dedicated bot, run it as 'fpv_bot'.")
    except Exception as e:
        print(f"[Host] Warning during environment sanitization: {e}")

    parser = argparse.ArgumentParser(description="Liftoff Headless Server Manager")
    parser.add_argument("--interval", type=int, default=600, help="Rotation interval in seconds (default: 600s / 10m).")
    parser.add_argument("--gui", action="store_true", help="Run the game with GUI enabled (on the host display, no Xvfb).")
    parser.add_argument("--playlist", type=str, default=None, help="Name of the playlist to load from playlists.json.")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle the tracks in the playlist before writing to rotation.")
    parser.add_argument("--lobby-name", type=str, default=None, help="Override the lobby name (room name) for the server.")
    parser.add_argument("--public", action="store_true", help="Make the lobby public instead of private.")
    parser.add_argument("--max-players", type=int, default=None, help="Max players allowed in the room (applied once the room is created).")
    parser.add_argument("--auto-start", action="store_true", help="Automatically start the race after players join, instead of staying in the lobby.")
    parser.add_argument("--width", type=int, default=640, help="Game window width (default: 640).")
    parser.add_argument("--height", type=int, default=480, help="Game window height (default: 480).")
    parser.add_argument("--log-file", type=str, default=None, help="Redirect Unity's Player.log to this path via -logFile, instead of the shared default location. Used to isolate concurrent instances' logs (see docs/features/doing/automated-testing.md).")
    args = parser.parse_args()

    config = load_config()
    logger = make_event_logger(config, project_dir)
    display = config.get("display", ":99")
    liftoff_path = os.path.expanduser(config.get("liftoff_path", ""))
    lobby_name = args.lobby_name if args.lobby_name else config.get("lobby_name", "Procedural Loop Room")
    
    # Auto-correct paths referencing another user's home (e.g. /home/fpv_bot vs /home/dev-user)
    if not os.path.exists(liftoff_path):
        import getpass
        current_user = getpass.getuser()
        parts = liftoff_path.split("/")
        if len(parts) > 2 and parts[1] == "home":
            parts[2] = current_user
            alternative_path = "/".join(parts)
            if os.path.exists(alternative_path):
                liftoff_path = alternative_path
                print(f"[Host] Auto-corrected liftoff path to current user's home: {liftoff_path}")

    # 1. Verify paths
    if not os.path.exists(liftoff_path):
        print(f"ERROR: Liftoff executable not found at: {liftoff_path}")
        sys.exit(1)

    game_dir = os.path.dirname(liftoff_path)
    plugins_dir = os.path.join(game_dir, "BepInEx", "plugins")
    os.makedirs(plugins_dir, exist_ok=True)

    # 2. Refresh track database before resolving playlists
    try:
        print("[Host] Gathering installed workshop and local tracks/races...")
        from gather_tracks import gather_tracks_and_races
        gather_tracks_and_races()
    except Exception as e:
        print(f"[Host] WARNING: Failed to update track list database: {e}")
        logger.error(f"Failed to update track list database: {e}", context="gather_tracks")

    # 3. Write rotation parameters to BepInEx plugin directory
    # The C# mod reads these files at runtime to configure itself
    print(f"[Host] Configuring rotation parameters:")
    print(f"  Lobby Name:  {lobby_name}")
    print(f"  Interval:    {args.interval}s")
    print(f"  Auto-start:  {args.auto_start}")

    with open(os.path.join(plugins_dir, "lobby_name.txt"), "w") as f:
        f.write(lobby_name)
    with open(os.path.join(plugins_dir, "rotation_interval.txt"), "w") as f:
        f.write(str(args.interval))
    with open(os.path.join(plugins_dir, "room_private.txt"), "w") as f:
        f.write("false" if args.public else "true")
    with open(os.path.join(plugins_dir, "auto_start.txt"), "w") as f:
        f.write("true" if args.auto_start else "false")
    with open(os.path.join(plugins_dir, "shuffle_mode.txt"), "w") as f:
        f.write("true" if args.shuffle else "false")
    if args.max_players is not None:
        with open(os.path.join(plugins_dir, "max_players.txt"), "w") as f:
            f.write(str(args.max_players))

    playlist_val = args.playlist if args.playlist else "custom"
    with open(os.path.join(plugins_dir, "playlist_name.txt"), "w") as f:
        f.write(playlist_val)

    # Write available playlists to available_playlists.txt
    playlists_path = os.path.join(project_dir, "playlists.json")
    if os.path.exists(playlists_path):
        try:
            with open(playlists_path, "r") as f:
                playlists_data = json.load(f)
            available_playlists_file = os.path.join(plugins_dir, "available_playlists.txt")
            with open(available_playlists_file, "w") as pf:
                for name in playlists_data.keys():
                    pf.write(f"{name}\n")
            print(f"[Host] Wrote available playlists list to {available_playlists_file}")
        except Exception as e:
            print(f"[Host] WARNING: Failed to write available_playlists.txt: {e}")

    # Set up tracks_to_rotate.txt
    tracks_file = os.path.join(plugins_dir, "tracks_to_rotate.txt")
    if args.playlist:
        try:
            resolve_and_write_playlist(args.playlist, args.shuffle, tracks_file, logger=logger)
        except ValueError as e:
            print(f"ERROR: {e}")
            logger.error(str(e), context="playlist_resolution", playlist=args.playlist)
            sys.exit(1)
    else:
        # Copy tracks_to_rotate.txt from current directory if it exists, otherwise create/keep existing
        local_tracks_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks_to_rotate.txt")
        if os.path.exists(local_tracks_file):
            import shutil
            print(f"[Host] Copying {local_tracks_file} to {tracks_file}")
            shutil.copy2(local_tracks_file, tracks_file)
        elif not os.path.exists(tracks_file):
            print(f"[Host] Creating initial empty tracks_to_rotate.txt at: {tracks_file}")
            with open(tracks_file, "w") as f:
                f.write("# Format: TrackName,EnvironmentName,GameModeName\n")

    # 4. Start Xvfb if not running and GUI mode is NOT enabled
    if not args.gui:
        print("[Host] Checking virtual framebuffer (Xvfb)...")
        xvfb_running = False
        try:
            out = run_command(["pgrep", "Xvfb"], check=False)
            if out:
                xvfb_running = True
                print("[Host] Xvfb is already running.")
        except Exception:
            pass
            
        if not xvfb_running:
            print(f"[Host] Starting Xvfb on display {display}...")
            subprocess.Popen(["Xvfb", display, "-screen", "0", "1280x720x24"])
            time.sleep(2)
    else:
        print("[Host] GUI mode enabled. Bypassing Xvfb startup.")

    # 5. Launch/Monitor Loop
    if args.gui:
        print("\n[Host] Starting Liftoff GUI server monitor loop...")
    else:
        print("\n[Host] Starting Liftoff headless server monitor loop...")
        
    env = os.environ.copy()
    if not args.gui:
        env["DISPLAY"] = display
    else:
        env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    env["STEAM_ENABLE_BOT"] = "1"  # Triggers BepInEx wrapper loader in launch.sh

    dbus_addr = get_steam_dbus_address()
    if dbus_addr:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr
        print(f"[Host] Injected Steam DBUS address: {dbus_addr}")

    # Set Steam App ID environment variables to ensure Steamworks API initializes
    env["SteamAppId"] = "410340"
    env["STEAM_APP_ID"] = "410340"
    env["SteamGameId"] = "410340"

    # Remove parent user session variables to prevent Steam IPC hijacking (XDG_RUNTIME_DIR/DBUS are already sanitized at startup)
    for var in ["XDG_SESSION_CLASS", "XDG_SESSION_TYPE", "XDG_SESSION_ID"]:
        env.pop(var, None)

    # Use BepInEx's run_bepinex.sh loader (immune to Steam validation updates)
    launch_sh_path = os.path.join(game_dir, "run_bepinex.sh")
    if not os.path.exists(launch_sh_path):
        print(f"ERROR: run_bepinex.sh loader script not found at: {launch_sh_path}")
        sys.exit(1)

    logger.orchestrator_start(args.interval, playlist=playlist_val, lobby_name=lobby_name,
                              gui=args.gui, auto_start=args.auto_start)

    try:
        proc = None
        last_process_check = 0.0
        active_playlist = playlist_val

        while True:
            current_time = time.time()
            
            # 1. Check for playlist change (every 1 second)
            playlist_name_path = os.path.join(plugins_dir, "playlist_name.txt")
            if os.path.exists(playlist_name_path):
                try:
                    with open(playlist_name_path, "r") as f:
                        current_playlist_name = f.read().strip()
                    if current_playlist_name and current_playlist_name != active_playlist:
                        print(f"[Host] Playlist change detected: '{active_playlist}' -> '{current_playlist_name}'")
                        logger.playlist_change(active_playlist, current_playlist_name)
                        # Read shuffle mode
                        shuffle_enabled = False
                        shuffle_mode_path = os.path.join(plugins_dir, "shuffle_mode.txt")
                        if os.path.exists(shuffle_mode_path):
                            with open(shuffle_mode_path, "r") as sf:
                                shuffle_enabled = (sf.read().strip().lower() == "true")

                        try:
                            resolve_and_write_playlist(current_playlist_name, shuffle_enabled, tracks_file, logger=logger)
                            active_playlist = current_playlist_name
                        except Exception as ex:
                            print(f"[Host] Failed to resolve and write playlist '{current_playlist_name}': {ex}")
                            logger.error(f"Failed to resolve and write playlist: {ex}",
                                         context="playlist_resolution", playlist=current_playlist_name)
                except Exception as e:
                    print(f"[Host] Error checking playlist change: {e}")

            # 2. Check process state and handle relaunch (every 15 seconds)
            if current_time - last_process_check >= 15.0:
                last_process_check = current_time
                pids = get_active_liftoff_pids()
                
                if proc is not None:
                    proc.poll()
                
                if pids:
                    print(f"[Host] Liftoff is running (PIDs: {', '.join(pids)}). Monitoring...")
                else:
                    # Check if maintenance mode is active
                    maintenance_active_path = os.path.join(plugins_dir, "maintenance_active.txt")
                    if os.path.exists(maintenance_active_path):
                        print("[Host] Liftoff exited and maintenance mode is active. Exiting orchestrator cleanly.")
                        logger.shutdown("maintenance")
                        try:
                            os.remove(maintenance_active_path)
                        except Exception:
                            pass
                        sys.exit(0)
                    
                    print("[Host] Liftoff is not running. Starting game server...")
                    launch_args = [
                        launch_sh_path,
                        os.path.join(game_dir, "Liftoff.x86_64"),
                        "-screen-width", str(args.width),
                        "-screen-height", str(args.height),
                        "-screen-fullscreen", "0"
                    ]
                    if args.log_file:
                        launch_args += ["-logFile", args.log_file]
                    proc = subprocess.Popen(launch_args, env=env, cwd=game_dir)
                    print(f"[Host] Started Liftoff server process (PID: {proc.pid}).")
                    logger.game_start(proc.pid, playlist=active_playlist,
                                      width=args.width, height=args.height)
            
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[Host] Stopped by user request. Exiting...")
        logger.shutdown("keyboard_interrupt")
        # Terminate any running Liftoff process
        try:
            run_command(["pkill", "-f", "Liftoff.x86_64"], check=False)
            print("[Host] Terminated Liftoff server process.")
        except Exception:
            pass
        sys.exit(0)

if __name__ == "__main__":
    main()
