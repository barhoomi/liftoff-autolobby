import os
import sys
import json
import time
import subprocess
import argparse

# The control plane -- playlist resolution plus every write to the plugin's plain-text
# protocol files -- lives in dashboard/control/ as of bot-dashboard.md decision D5: the
# dashboard owns it and this orchestrator is one of its two callers. There is exactly one
# implementation of each; nothing was left behind here.
#
# dashboard/ sits at the repo root, which is NOT on sys.path when this file is run as
# `python3 orchestrator/run_headless_lobby.py` (Python only adds the script's own
# directory), so bootstrap the repo root first -- the same pattern gather_tracks.py uses
# for `import trackcheck`.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dashboard.control import (  # noqa: E402  (import needs the bootstrap above)
    EVENT_LOG_AVAILABLE,
    MasterTracksMissingError,
    PlaylistError,
    ProtocolDir,
    TrackBootstrap,
    bootstrap_timeout,
    load_lobby_config,
    make_event_logger,
    master_list_has_tracks,
    master_tracks_path,
    resolve_and_write_playlist,
    resolve_liftoff_path,
    resolve_log_dir,
)


def load_config():
    """lobby_config.json, with the orchestrator's historical fatal-on-missing behavior.

    The parse itself lives in the control plane (one implementation); the ``sys.exit``
    stays here because it is a CLI decision, not a library one -- the dashboard imports
    the same loader and degrades to defaults instead of killing its own process.
    """
    try:
        return load_lobby_config(_PROJECT_ROOT)
    except FileNotFoundError as e:
        print(f"ERROR: Configuration file not found at {e}")
        sys.exit(1)

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
    parser.add_argument("--democracy", action="store_true", help="Enable democracy mode for track skipping.")
    parser.add_argument("--width", type=int, default=640, help="Game window width (default: 640).")
    parser.add_argument("--height", type=int, default=480, help="Game window height (default: 480).")
    parser.add_argument("--log-file", type=str, default=None, help="Redirect Unity's Player.log to this path via -logFile, instead of the shared default location. Used to isolate concurrent instances' logs (see docs/features/doing/automated-testing.md).")
    args = parser.parse_args()

    config = load_config()
    logger = make_event_logger(config, project_dir)
    display = config.get("display", ":99")
    lobby_name = args.lobby_name if args.lobby_name else config.get("lobby_name", "Procedural Loop Room")

    # Resolved by the control plane (which also auto-corrects a path pointing at another
    # user's home, e.g. /home/fpv_bot vs the dev user's home) so the dashboard resolves the
    # game install exactly the same way this process does.
    configured_path = os.path.expanduser(config.get("liftoff_path", "") or "")
    liftoff_path = resolve_liftoff_path(config)
    if liftoff_path != configured_path:
        print(f"[Host] Auto-corrected liftoff path to current user's home: {liftoff_path}")

    # 1. Verify paths
    if not os.path.exists(liftoff_path):
        print(f"ERROR: Liftoff executable not found at: {liftoff_path}")
        sys.exit(1)

    game_dir = os.path.dirname(liftoff_path)
    plugins_dir = os.path.join(game_dir, "BepInEx", "plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    # Every protocol-file write below goes through the control plane: atomic writes the
    # plugin's 1-second poll can't tear, an flock shared with the dashboard process, and
    # an ownership table that refuses to content-write plugin-owned state.
    protocol = ProtocolDir(plugins_dir)

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
    print(f"  Democracy:   {args.democracy}")

    protocol.set_lobby_name(lobby_name)
    protocol.set_rotation_interval(args.interval)
    protocol.set_room_private(not args.public)
    protocol.set_auto_start(args.auto_start)
    protocol.set_shuffle_mode(args.shuffle)
    protocol.set_democracy_mode(args.democracy)
    if args.max_players is not None:
        protocol.set_max_players(args.max_players)

    # Structured-logging (A3): hand the plugin the SAME resolved log directory the
    # orchestrator uses, via a state file (plain-text plugin<->orchestrator protocol).
    # The plugin runs from the game install and has no notion of the repo root, so it
    # must be TOLD the dir. resolve_log_dir is the single resolver (CLAUDE rule #4) —
    # the plugin reads this value rather than re-deriving the directory a second way.
    if EVENT_LOG_AVAILABLE:
        try:
            protocol.set_log_dir(resolve_log_dir(config, project_dir))
        except Exception as e:
            print(f"[Host] WARNING: Failed to write log_dir.txt: {e}")
            logger.error(f"Failed to write log_dir.txt: {e}", context="log_dir_state")

    playlist_val = args.playlist if args.playlist else "custom"
    protocol.set_playlist_name(playlist_val)

    # Write available playlists to available_playlists.txt
    playlists_file = os.path.join(project_dir, "config", "playlists.json")
    if os.path.exists(playlists_file):
        try:
            with open(playlists_file, "r") as f:
                playlists_data = json.load(f)
            protocol.set_available_playlists(playlists_data.keys())
            print(f"[Host] Wrote available playlists list to {protocol.path('available_playlists.txt')}")
        except Exception as e:
            print(f"[Host] WARNING: Failed to write available_playlists.txt: {e}")

    # Set up tracks_to_rotate.txt
    tracks_file = protocol.path("tracks_to_rotate.txt")

    # First-run track bootstrap (docs/features/doing/fresh-install-track-bootstrap-deadlock.md,
    # option 3). On a genuinely fresh install the step-2 gather above finds nothing --
    # official tracks are baked into Unity asset bundles, so they are only ever discovered
    # by reconciling the dump the *plugin* writes while driving the settings popup. Detect
    # that state HERE, before resolution, and switch from "fail/serve an empty rotation
    # forever" to "launch the game, wait for the dump, then redo gather + resolve".
    # `bootstrap_timeout() <= 0` opts out and keeps the historical fail-fast behaviour.
    bootstrap = None
    bootstrap_seconds = bootstrap_timeout()
    bootstrap_needed = (args.playlist is not None and bootstrap_seconds > 0
                        and not master_list_has_tracks(master_tracks_path(project_dir)))

    if args.playlist:
        try:
            resolve_and_write_playlist(args.playlist, args.shuffle, tracks_file, logger=logger)
        except MasterTracksMissingError as e:
            # Absent (not merely empty) master list. Historically fatal; now fatal only
            # when the bootstrap is unavailable / opted out of, since the bootstrap exists
            # precisely to fill this file in.
            if not bootstrap_needed:
                print(f"ERROR: {e}")
                logger.error(str(e), context="playlist_resolution", playlist=args.playlist)
                sys.exit(1)
            print(f"[Host] {e} — deferring playlist resolution to the first-run bootstrap.")
            if not os.path.exists(tracks_file):
                protocol.write_text(os.path.basename(tracks_file),
                                    "# Format: TrackName,EnvironmentName,GameModeName\n")
        except (ValueError, PlaylistError) as e:
            # PlaylistError covers the master_tracks_list.json-missing case, which used to
            # sys.exit(1) from inside the resolver itself. Same message, same exit code --
            # but the decision now lives at the CLI call site, not in the library.
            print(f"ERROR: {e}")
            logger.error(str(e), context="playlist_resolution", playlist=args.playlist)
            sys.exit(1)

        if bootstrap_needed:
            bootstrap = TrackBootstrap(plugins_dir, args.playlist, tracks_file,
                                       shuffle=args.shuffle, logger=logger,
                                       timeout=bootstrap_seconds)
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
            
            # 1. Check for playlist change (every 1 second). The writer may be the plugin
            # (/playlist) or the dashboard -- both write playlist_name.txt and let this
            # loop do the resolution.
            if protocol.exists("playlist_name.txt"):
                try:
                    current_playlist_name = protocol.read_text("playlist_name.txt", "")
                    if current_playlist_name and current_playlist_name != active_playlist:
                        print(f"[Host] Playlist change detected: '{active_playlist}' -> '{current_playlist_name}'")
                        logger.playlist_change(active_playlist, current_playlist_name)
                        shuffle_enabled = protocol.read_flag("shuffle_mode.txt")

                        try:
                            resolve_and_write_playlist(current_playlist_name, shuffle_enabled, tracks_file, logger=logger)
                            active_playlist = current_playlist_name
                        except Exception as ex:
                            print(f"[Host] Failed to resolve and write playlist '{current_playlist_name}': {ex}")
                            logger.error(f"Failed to resolve and write playlist: {ex}",
                                         context="playlist_resolution", playlist=current_playlist_name)
                except Exception as e:
                    print(f"[Host] Error checking playlist change: {e}")

            # 1b. First-run track bootstrap: once the game is up, watch for the plugin's
            # Environment x GameMode dump and, the moment it lands, regenerate
            # master_tracks_list.json and re-resolve the playlist. Self-rate-limiting, so
            # calling it every tick is free; it retires itself (active -> False) on
            # completion, timeout or failure, and the loop keeps servicing everything else
            # throughout -- deliberately not a blocking wait.
            if bootstrap is not None and bootstrap.active:
                bootstrap.poll()
                if bootstrap.state == TrackBootstrap.COMPLETED:
                    # The bootstrap resolved whatever playlist was active when it armed.
                    # Pointing active_playlist back at that name (rather than assuming it
                    # is still current) means an operator who switched playlists mid-boot
                    # simply gets picked up by check 1 above on the very next tick, now
                    # that the master list is populated -- no special case needed.
                    active_playlist = bootstrap.playlist_name

            # 2. Check process state and handle relaunch (every 15 seconds)
            if current_time - last_process_check >= 15.0:
                last_process_check = current_time
                pids = get_active_liftoff_pids()
                
                if proc is not None:
                    proc.poll()
                
                if pids:
                    print(f"[Host] Liftoff is running (PIDs: {', '.join(pids)}). Monitoring...")
                    # A game we did not launch ourselves (adopted from a previous
                    # orchestrator run) still produces the dump, so start the clock here
                    # too -- otherwise the bootstrap would sit idle forever.
                    if bootstrap is not None:
                        bootstrap.note_game_started()
                else:
                    # Check if maintenance mode is active
                    if protocol.exists("maintenance_active.txt"):
                        print("[Host] Liftoff exited and maintenance mode is active. Exiting orchestrator cleanly.")
                        logger.shutdown("maintenance")
                        try:
                            protocol.set_maintenance(False)
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
                    # The bootstrap's timeout budget starts here, not at orchestrator
                    # startup: everything it waits on is produced by this process.
                    if bootstrap is not None:
                        bootstrap.note_game_started()
            
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
