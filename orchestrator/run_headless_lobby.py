import os
import sys
import json
import time
import subprocess
import argparse
import fnmatch

def resolve_and_write_playlist(playlist_name, shuffle_enabled, output_file):
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
        print(f"ERROR: Playlist '{playlist_name}' not found in playlists.json. Available: {list(playlists_data.keys())}")
        sys.exit(1)
        
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
                
            # Environment matches, match tracks in shareable categories only (not local)
            for category in ["official", "workshop", "custom"]:
                if category not in master_data[env_key]:
                    continue
                for track_name in master_data[env_key][category]:
                    if is_match(track_pattern, track_name):
                        track_entry = (track_name, env_key, game_mode)
                        if track_entry not in resolved_tracks:
                            resolved_tracks.append(track_entry)
                        
    if not resolved_tracks:
        print(f"WARNING: Playlist '{playlist_name}' resolved to 0 tracks from master list.")
    else:
        print(f"[Playlist] Resolved {len(resolved_tracks)} tracks for playlist '{playlist_name}'.")
        
    if shuffle_enabled:
        print("[Playlist] Shuffling track rotation order...")
        random.shuffle(resolved_tracks)
    else:
        print("[Playlist] Using track rotation in playlist definition order...")
        
    # Write to tracks_to_rotate.txt
    with open(output_file, "w") as f:
        f.write("# Generated from playlist: " + playlist_name + "\n")
        for track_name, ui_env, game_mode in resolved_tracks:
            f.write(f"{track_name},{ui_env},{game_mode}\n")
    print(f"[Playlist] Wrote tracks to rotate to: {output_file}")

    # Reset rotation state to 0
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

def main():
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
    parser.add_argument("--auto-start", action="store_true", help="Automatically start the race after players join, instead of staying in the lobby.")
    args = parser.parse_args()

    config = load_config()
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
    
    playlist_val = args.playlist if args.playlist else "custom"
    with open(os.path.join(plugins_dir, "playlist_name.txt"), "w") as f:
        f.write(playlist_val)

    # Set up tracks_to_rotate.txt
    tracks_file = os.path.join(plugins_dir, "tracks_to_rotate.txt")
    if args.playlist:
        resolve_and_write_playlist(args.playlist, args.shuffle, tracks_file)
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

    # Remove parent user session variables to prevent Steam IPC hijacking (XDG_RUNTIME_DIR/DBUS are already sanitized at startup)
    for var in ["XDG_SESSION_CLASS", "XDG_SESSION_TYPE", "XDG_SESSION_ID"]:
        env.pop(var, None)

    # Use BepInEx's run_bepinex.sh loader (immune to Steam validation updates)
    launch_sh_path = os.path.join(game_dir, "run_bepinex.sh")
    if not os.path.exists(launch_sh_path):
        print(f"ERROR: run_bepinex.sh loader script not found at: {launch_sh_path}")
        sys.exit(1)

    try:
        proc = None
        while True:
            # Check if Liftoff is currently running (excluding defunct ones)
            pids = get_active_liftoff_pids()
            
            # Reap our child process if it exited to prevent zombies
            if proc is not None:
                proc.poll()

            if pids:
                print(f"[Host] Liftoff is running (PIDs: {', '.join(pids)}). Monitoring...")
            else:
                print("[Host] Liftoff is not running. Starting game server...")
                # Start game using run_bepinex.sh
                proc = subprocess.Popen([
                    launch_sh_path,
                    os.path.join(game_dir, "Liftoff.x86_64"),
                    "-screen-width", "640",
                    "-screen-height", "480",
                    "-screen-fullscreen", "0"
                ], env=env, cwd=game_dir)
                print(f"[Host] Started Liftoff server process (PID: {proc.pid}).")
            
            # Sleep 15 seconds before checking again
            time.sleep(15)

    except KeyboardInterrupt:
        print("\n[Host] Stopped by user request. Exiting...")
        # Terminate any running Liftoff process
        try:
            run_command(["pkill", "-f", "Liftoff.x86_64"], check=False)
            print("[Host] Terminated Liftoff server process.")
        except Exception:
            pass
        sys.exit(0)

if __name__ == "__main__":
    main()
