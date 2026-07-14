import os
import sys
import glob
import json
import getpass
import re

# trackcheck/ lives at the repo root (two levels up from this file); it's not on
# sys.path by default when this script is run as `python3 orchestrator/gather_tracks.py`
# (Python only puts the script's own directory on sys.path). Bootstrap the repo root
# so `import trackcheck` resolves regardless of caller cwd -- same pattern as the
# config_path lookup in gather_tracks_and_races() below.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# The robust XML parser (encoding fallback, declaration stripping, localID/name/
# environment, race TRACK dependency) now lives in trackcheck/parser.py -- one
# implementation shared with the Layer 1/2/3 validation library (see
# docs/features/doing/track-validation-quality-gate.md). Re-exported at module level
# so `from gather_tracks import normalize_env` (used by orchestrator/tests/test_gather_tracks.py)
# keeps working unchanged.
from trackcheck.parser import ENV_MAPPING, normalize_env, parse_xml_robust, parse_track_file, parse_race_file  # noqa: F401

OFFICIAL_TRACK_MAPPING = {
    "default": "01 - The Biggest Yet",
    "dronehub": "02 - Bring Me A Shrubbery",
    "dronehub1": "03 - Cone Off"
}

def is_tutorial(name):
    if not name:
        return False
    ln = name.lower()
    return ln.startswith("tutorial") or ln.startswith("learning")

def gather_tracks_and_races():
    current_user = getpass.getuser()
    
    # 1. Determine paths
    # Custom
    custom_tracks_dir = os.path.expanduser("~/.config/unity3d/LuGus Studios/Liftoff/Tracks")
    custom_races_dir = os.path.expanduser("~/.config/unity3d/LuGus Studios/Liftoff/Races")
    
    # Workshop paths
    workshop_candidates = [
        f"/home/{current_user}/.steam/debian-installation/steamapps/workshop/content/410340",
        f"/home/{current_user}/.steam/steam/steamapps/workshop/content/410340",
        f"/home/{current_user}/.local/share/Steam/steamapps/workshop/content/410340"
    ]
    workshop_dir = None
    for path in workshop_candidates:
        if os.path.exists(path):
            workshop_dir = path
            break
            
    # Game installation paths
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lobby_config.json")
    game_tracks_dir = None
    game_races_dir = None
    game_dir = None
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            liftoff_path = config.get("liftoff_path", "")
            # Correct path if it has reference to another user home
            parts = liftoff_path.split("/")
            if len(parts) > 2 and parts[1] == "home":
                parts[2] = current_user
                liftoff_path = "/".join(parts)
            if os.path.exists(liftoff_path):
                game_dir = os.path.dirname(liftoff_path)
                game_tracks_dir = os.path.join(game_dir, "Liftoff_Data", "Tracks")
                game_races_dir = os.path.join(game_dir, "Liftoff_Data", "Races")
        except Exception:
            pass

    # Gather search patterns and classify them
    track_patterns = []
    if os.path.exists(custom_tracks_dir):
        track_patterns.append((os.path.join(custom_tracks_dir, "**/*.track"), "local"))
    if workshop_dir:
        track_patterns.append((os.path.join(workshop_dir, "**/*.track"), "workshop"))
    if game_tracks_dir and os.path.exists(game_tracks_dir):
        track_patterns.append((os.path.join(game_tracks_dir, "**/*.track"), "official"))
        
    race_patterns = []
    if os.path.exists(custom_races_dir):
        race_patterns.append(os.path.join(custom_races_dir, "**/*.race"))
    if workshop_dir:
        race_patterns.append(os.path.join(workshop_dir, "**/*.race"))
    if game_races_dir and os.path.exists(game_races_dir):
        race_patterns.append(os.path.join(game_races_dir, "**/*.race"))

    # Parse tracks
    tracks = {}
    for pattern, category in track_patterns:
        for filepath in glob.glob(pattern, recursive=True):
            res = parse_track_file(filepath)
            if res:
                local_id, name, env = res
                if local_id and name and env:
                    if is_tutorial(name):
                        continue
                    if category == "official":
                        name = OFFICIAL_TRACK_MAPPING.get(name, name)
                    tracks[local_id] = {
                        'name': name,
                        'environment': normalize_env(env),
                        'category': category
                    }

    # Parse races
    races = {}
    for pattern in race_patterns:
        for filepath in glob.glob(pattern, recursive=True):
            res = parse_race_file(filepath)
            if res:
                name, track_dep = res
                if name and track_dep:
                    if is_tutorial(name):
                        continue
                    races.setdefault(track_dep, []).append(name)

    # Clean duplicates in races
    for tid in races:
        races[tid] = sorted(list(set(races[tid])))

    # 2. Merge with UI dump if available, otherwise fallback to old behavior
    #
    # Derive from `game_dir` (already resolved above from lobby_config.json's liftoff_path,
    # with home-directory auto-correction) rather than hardcoding the Debian .deb Steam
    # package's `~/.steam/debian-installation/...` layout a second time. That hardcode was a
    # second, independent path guess for the same install lobby_config.json already told us
    # about (AGENTS.md rule 4: single source of truth) -- it silently found nothing whenever
    # the install lived anywhere else (e.g. a containerized/steamcmd `force_install_dir`
    # layout, see docs/features/doing/docker-container.md). Fall back to the old guess only
    # when liftoff_path didn't resolve at all, to preserve old-host behavior.
    if game_dir:
        ui_dump_path = os.path.join(game_dir, "BepInEx", "plugins", "ui_tracks_dump.json")
    else:
        ui_dump_path = os.path.expanduser("~/.steam/debian-installation/steamapps/common/Liftoff/BepInEx/plugins/ui_tracks_dump.json")
    ui_dump_data = None
    if os.path.exists(ui_dump_path):
        try:
            with open(ui_dump_path, "r") as f:
                ui_dump_data = json.load(f)
            print(f"[Host] Found UI tracks dump at {ui_dump_path}. Reconciling...")
        except Exception as e:
            print(f"[Host] WARNING: Failed to load UI tracks dump: {e}")

    master_list_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "master_tracks_list.json")
    if ui_dump_data:
        master_data = {}
        for env_ui, tracks_ui in ui_dump_data.items():
            env_normalized = normalize_env(env_ui)
            if env_normalized not in master_data:
                master_data[env_normalized] = {"official": {}, "workshop": {}, "local": {}}

            for track_name_ui in tracks_ui:
                # Find matching parsed track from files to get races AND source category
                matching_id = None
                matched_category = None
                # First try exact match (case insensitive)
                for tid, tinfo in tracks.items():
                    if tinfo['environment'] == env_normalized and tinfo['name'].lower() == track_name_ui.lower():
                        matching_id = tid
                        matched_category = tinfo['category']
                        break
                # Fallback to substring match
                if not matching_id:
                    for tid, tinfo in tracks.items():
                        if tinfo['environment'] == env_normalized:
                            if tinfo['name'].lower() in track_name_ui.lower() or track_name_ui.lower() in tinfo['name'].lower():
                                matching_id = tid
                                matched_category = tinfo['category']
                                break

                # Determine category: prefer file-based source, fall back to name heuristic
                if matched_category:
                    category = matched_category
                else:
                    is_official = bool(re.match(r"^\d+ - ", track_name_ui)) and env_normalized != "The Drawing Board"
                    category = "official" if is_official else "workshop"

                track_races = races.get(matching_id, []) if matching_id else []
                master_data[env_normalized][category][track_name_ui] = track_races
    else:
        # Fallback to path-based merge if no UI dump is available
        if os.path.exists(master_list_path):
            try:
                with open(master_list_path, "r") as f:
                    master_data = json.load(f)
                # Migrate any old "custom" keys to "workshop"
                for env_key in master_data:
                    if isinstance(master_data[env_key], dict) and "custom" in master_data[env_key]:
                        master_data[env_key].setdefault("workshop", {}).update(master_data[env_key].pop("custom"))
            except Exception:
                master_data = {}
        else:
            master_data = {}

        for local_id, info in tracks.items():
            env = info['environment']
            name = info['name']
            category = info['category']  # "local", "workshop", or "official"
            track_races = races.get(local_id, [])

            if env not in master_data or not isinstance(master_data[env], dict):
                master_data[env] = {"official": {}, "workshop": {}, "local": {}}
            else:
                for cat in ("official", "workshop", "local"):
                    master_data[env].setdefault(cat, {})

            cat_dict = master_data[env][category]
            if name not in cat_dict:
                cat_dict[name] = track_races
            else:
                existing_races = cat_dict[name]
                if not isinstance(existing_races, list):
                    existing_races = []
                merged = sorted(list(set(existing_races + track_races)))
                cat_dict[name] = merged

    # Clean up any residual keys (e.g. old "custom" from a previous schema)
    for env_key in master_data:
        if isinstance(master_data[env_key], dict):
            keys_to_remove = [k for k in master_data[env_key] if k not in ("official", "workshop", "local")]
            for k in keys_to_remove:
                del master_data[env_key][k]

    # Write back updated master list
    with open(master_list_path, "w") as f:
        json.dump(master_data, f, indent=2)

    print(f"Successfully updated {master_list_path}")
    print(f"Scanned tracks count: {len(tracks)}")

if __name__ == "__main__":
    gather_tracks_and_races()
