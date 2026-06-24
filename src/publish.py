import os
import json
import shutil
import subprocess
import re
import sys

PROJECT_WORKSPACE = "/home/dev-user/Projects/procedural-fpv"
STAGING_DIR = os.path.join(PROJECT_WORKSPACE, "workshop_staging")
VDF_PATH = os.path.join(PROJECT_WORKSPACE, "workshop_build.vdf")
CONFIG_PATH = os.path.join(PROJECT_WORKSPACE, "workshop_config.json")
PREVIEW_PATH = os.path.join(PROJECT_WORKSPACE, "preview.jpg")

STEAMCMD_PATH = "/usr/games/steamcmd"
STEAM_APP_ID = "410340" # Liftoff App ID

def load_workshop_config():
    """
    Loads saved workshop item configurations.
    """
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"published_file_id": "0"}

def save_workshop_config(config):
    """
    Saves workshop configurations.
    """
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)

def stage_files(track_id):
    """
    Clears the staging directory and copies both the .track and .race files into it.
    """
    # Create or clean staging dir
    if os.path.exists(STAGING_DIR):
        shutil.rmtree(STAGING_DIR)
    os.makedirs(STAGING_DIR, exist_ok=True)
    
    # Paths in Liftoff config
    liftoff_base = os.path.expanduser("~/.config/unity3d/LuGus Studios/Liftoff")
    track_dir = os.path.join(liftoff_base, "Tracks", track_id)
    
    # Locate race dir matching track_id prefix dynamically
    races_parent_dir = os.path.join(liftoff_base, "Races")
    race_dir = None
    if os.path.exists(races_parent_dir):
        matching_dirs = [
            os.path.join(races_parent_dir, d)
            for d in os.listdir(races_parent_dir)
            if d.startswith(f"{track_id}_race") and os.path.isdir(os.path.join(races_parent_dir, d))
        ]
        if matching_dirs:
            # Sort by modification time to pick the newest one
            matching_dirs.sort(key=os.path.getmtime)
            race_dir = matching_dirs[-1]
            
    if not race_dir:
        race_dir = os.path.join(races_parent_dir, f"{track_id}_race")
    
    # Locate files
    track_files = [f for f in os.listdir(track_dir) if f.endswith(".track")] if os.path.exists(track_dir) else []
    race_files = [f for f in os.listdir(race_dir) if f.endswith(".race")] if os.path.exists(race_dir) else []
    
    if not track_files:
        raise ValueError(f"No .track files found in {track_dir}")
    if not race_files:
        raise ValueError(f"No .race files found in {race_dir}")
        
    # Copy files directly to staging root
    for tf in track_files:
        shutil.copy2(os.path.join(track_dir, tf), os.path.join(STAGING_DIR, tf))
    for rf in race_files:
        shutil.copy2(os.path.join(race_dir, rf), os.path.join(STAGING_DIR, rf))
        
    print(f"[Publish] Staged files for track '{track_id}' from {track_dir} and race from {race_dir} in: {STAGING_DIR}")

def write_vdf(published_file_id):
    """
    Writes the Steam VDF file for workshop publishing.
    """
    vdf_content = f"""\
"workshopitem"
{{
  "appid" "{STEAM_APP_ID}"
  "publishedfileid" "{published_file_id}"
  "contentfolder" "{STAGING_DIR}"
  "previewfile" "{PREVIEW_PATH}"
  "title" "Procedural FPV Loop"
  "description" "Procedurally generated FPV track & race loop by fpv_bot"
  "changenote" "Automatic updates from generator"
  "visibility" "0"
}}
"""
    with open(VDF_PATH, "w") as f:
        f.write(vdf_content)
    print(f"[Publish] Generated VDF file at: {VDF_PATH}")

def run_steamcmd():
    """
    Runs steamcmd to upload the workshop item.
    """
    cmd = [
        STEAMCMD_PATH,
        "+login", "fpv_bot",
        "+workshop_build_item", VDF_PATH,
        "+quit"
    ]
    
    print("[Publish] Initiating SteamCMD upload...")
    print(f"[Publish] Command: {' '.join(cmd)}")
    
    # Run SteamCMD and capture output
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    stdout_lines = []
    while True:
        line = process.stdout.readline()
        if not line:
            break
        # Print output in real-time
        sys.stdout.write(f"  [SteamCMD] {line}")
        sys.stdout.flush()
        stdout_lines.append(line)
        
    process.wait()
    stdout = "".join(stdout_lines)
    
    if process.returncode != 0:
        raise RuntimeError(f"SteamCMD failed with exit code: {process.returncode}")
        
    return stdout

def publish_track(track_id):
    """
    Full pipeline to stage and publish track to Steam Workshop.
    """
    # 1. Stage the files
    stage_files(track_id)
    
    # 2. Load configurations
    config = load_workshop_config()
    published_file_id = config.get("published_file_id", "0")
    
    # 3. Generate VDF
    write_vdf(published_file_id)
    
    # 4. Upload
    stdout = run_steamcmd()
    
    # 5. Extract publishedfileid if we created a new item
    if published_file_id == "0" or published_file_id == 0:
        # Search for "Created new item with ID <id>" or "ID <id>"
        match = re.search(r"Created new item with ID (\d+)", stdout)
        if not match:
            # Fallback regex
            match = re.search(r"ID\s+(\d+)", stdout)
            
        if match:
            new_id = match.group(1)
            config["published_file_id"] = new_id
            save_workshop_config(config)
            print(f"\n[Publish] SUCCESS! Created new Steam Workshop item with ID: {new_id}")
            print(f"[Publish] Workshop URL: https://steamcommunity.com/sharedfiles/filedetails/?id={new_id}")
        else:
            print("\n[Publish] WARNING: Upload completed but could not parse the new Workshop Item ID from SteamCMD output.", file=sys.stderr)
    else:
        print(f"\n[Publish] SUCCESS! Updated existing Steam Workshop item with ID: {published_file_id}")
        print(f"[Publish] Workshop URL: https://steamcommunity.com/sharedfiles/filedetails/?id={published_file_id}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 publish.py <track_id>")
        sys.exit(1)
    publish_track(sys.argv[1])
