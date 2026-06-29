import os
import glob
import json
import xml.etree.ElementTree as ET
import getpass
import re

# Normalization map for Liftoff environment names
ENV_MAPPING = {
    "TheDrawingBoard": "The Drawing Board",
    "The Drawing Board": "The Drawing Board",
    "TheDrawingBoardCyber": "The Drawing Board",
    "TheGreen": "The Green",
    "The Green": "The Green",
    "Hannover": "Hannover",
    "Hall26": "Hall 26",
    "Hall 26": "Hall 26",
    "AutumnFields": "Autumn Fields",
    "Autumn Fields": "Autumn Fields",
    "BandoCity": "Bando City",
    "Bando City": "Bando City",
    "HangarC03": "Hangar C03",
    "Hangar C03": "Hangar C03",
    "LiftoffArena": "Liftoff Arena",
    "Liftoff Arena": "Liftoff Arena",
    "PineValley": "Pine Valley",
    "Pine Valley": "Pine Valley",
    "StrawBale": "Straw Bale",
    "Straw Bale": "Straw Bale",
    "MinusTwo": "Minus Two",
    "Minus Two": "Minus Two",
    "DubaiLegends": "Dubai Legends",
    "Dubai Legends": "Dubai Legends",
    "ParisDroneFestival": "Paris Drone Festival",
    "Paris Drone Festival": "Paris Drone Festival",
    "ThePit": "The Pit",
    "The Pit": "The Pit",
    "BardwellsYard": "Bardwell's Yard",
    "Bardwell's Yard": "Bardwell's Yard",
    "RussianWoodpecker": "The Woodpecker",
    "Russian Woodpecker": "The Woodpecker",
    "TheWoodpecker": "The Woodpecker",
    "The Woodpecker": "The Woodpecker",
    "ShortCircuit": "Short Circuit",
    "Short Circuit": "Short Circuit",
    "Surtur": "Surtur",
    "Permafrost": "Permafrost",
    "Rustline": "Rustline",
    "MarinaBay": "Marina Bay",
    "Marina Bay": "Marina Bay",
    "AzureDistrict": "Azure District",
    "MelonPanPark": "Melon Pan Park"
}

OFFICIAL_TRACK_MAPPING = {
    "default": "01 - The Biggest Yet",
    "dronehub": "02 - Bring Me A Shrubbery",
    "dronehub1": "03 - Cone Off"
}

def normalize_env(env):
    if not env:
        return "Unknown"
    normalized = ENV_MAPPING.get(env)
    if normalized:
        return normalized
    # Fallback formatting: CamelCase to space-separated words
    spaced = re.sub(r'(?<!^)(?=[A-Z])', ' ', env)
    return spaced

def is_tutorial(name):
    if not name:
        return False
    ln = name.lower()
    return ln.startswith("tutorial") or ln.startswith("learning")

def parse_xml_robust(filepath):
    with open(filepath, 'rb') as f:
        content_bytes = f.read()
    try:
        content = content_bytes.decode('utf-8')
    except UnicodeDecodeError:
        content = content_bytes.decode('utf-16', errors='ignore')
    # Strip XML declaration's encoding parameter to avoid strict ElementTree parsing exceptions
    content = re.sub(r'<\?xml[^>]*encoding\s*=\s*["\'][^"\']*["\'][^>]*\?>', '<?xml version="1.0"?>', content, flags=re.IGNORECASE)
    return ET.fromstring(content)

def parse_track_file(filepath):
    try:
        root = parse_xml_robust(filepath)
        
        name_elem = root.find('name')
        name = name_elem.text.strip() if name_elem is not None and name_elem.text else None
        
        env_elem = root.find('environment')
        env = env_elem.text.strip() if env_elem is not None and env_elem.text else None
        
        local_id = None
        local_id_elem = root.find('.//localID/str')
        if local_id_elem is not None and local_id_elem.text:
            local_id = local_id_elem.text.strip()
        else:
            local_id_elem = root.find('.//localID')
            if local_id_elem is not None and local_id_elem.text:
                local_id = local_id_elem.text.strip()
                
        return local_id, name, env
    except Exception:
        return None

def parse_race_file(filepath):
    try:
        root = parse_xml_robust(filepath)
        
        name_elem = root.find('name')
        name = name_elem.text.strip() if name_elem is not None and name_elem.text else None
        
        track_dep = None
        for dep in root.findall('.//dependency'):
            dep_type = dep.find('type')
            if dep_type is not None and dep_type.text == 'TRACK':
                dep_str = dep.find('str')
                if dep_str is not None and dep_str.text:
                    track_dep = dep_str.text.strip()
                    break
                    
        return name, track_dep
    except Exception:
        return None

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
