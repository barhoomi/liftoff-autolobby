import os
import json
import shutil
import subprocess
import re
import sys

# generator/ — staging, vdf, and steamcmd artifacts are generated in here (gitignored);
# the committed preview thumbnail lives in generator/assets/.
PROJECT_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STAGING_DIR = os.path.join(PROJECT_WORKSPACE, "workshop_staging")
VDF_PATH = os.path.join(PROJECT_WORKSPACE, "workshop_build.vdf")
CONFIG_PATH = os.path.join(PROJECT_WORKSPACE, "workshop_config.json")
PREVIEW_PATH = os.path.join(PROJECT_WORKSPACE, "assets", "preview.jpg")

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

def write_vdf(published_file_id, track_id):
    """
    Writes the Steam VDF file for workshop publishing.
    """
    # Clean up track_id to make it a presentable title (e.g. "proc_loop_1" -> "Proc Loop 1")
    title_suffix = track_id.replace("_", " ").title()
    title = f"Procedural FPV - {title_suffix}"
    
    vdf_content = f"""\
"workshopitem"
{{
  "appid" "{STEAM_APP_ID}"
  "publishedfileid" "{published_file_id}"
  "contentfolder" "{STAGING_DIR}"
  "previewfile" "{PREVIEW_PATH}"
  "title" "{title}"
  "description" "Procedurally generated FPV track & race loop by fpv_bot"
  "changenote" "Automatic updates from generator"
  "visibility" "3"
}}
"""
    with open(VDF_PATH, "w") as f:
        f.write(vdf_content)
    print(f"[Publish] Generated VDF file at: {VDF_PATH}")

def run_steamcmd():
    """
    Runs steamcmd to upload the workshop item.
    """
    import os
    
    # 1. Create a dedicated home directory for SteamCMD to isolate it from the regular Steam client.
    # This prevents logins from conflicting with the user's primary gaming account (e.g., logging them out).
    steamcmd_home = os.path.abspath(os.path.join(PROJECT_WORKSPACE, ".steamcmd_home"))
    os.makedirs(steamcmd_home, exist_ok=True)
    
    # Clone current environment and override HOME and XDG variables
    env = os.environ.copy()
    env["HOME"] = steamcmd_home
    env["STEAM_HOME"] = steamcmd_home
    env["XDG_DATA_HOME"] = os.path.join(steamcmd_home, ".local/share")
    env["XDG_CONFIG_HOME"] = os.path.join(steamcmd_home, ".config")
    env["XDG_CACHE_HOME"] = os.path.join(steamcmd_home, ".cache")

    # Use @NoPromptForPassword to fail quickly and prevent the Python process from hanging
    # when waiting on stdin for a password prompt (due to readline buffering).
    cmd = [
        STEAMCMD_PATH,
        "+@NoPromptForPassword", "1",
        "+login", "fpv_bot",
        "+workshop_build_item", VDF_PATH,
        "+quit"
    ]
    
    print("[Publish] Initiating SteamCMD upload...")
    print(f"[Publish] Command: {' '.join(cmd)}")
    
    # Run SteamCMD and capture output
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env
        )
        
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

    except Exception as e:
        print("\n" + "="*80)
        print("[Publish] ERROR: SteamCMD execution failed or returned an error.")
        print(f"Details: {e}")
        print("-"*80)
        print("This is likely because SteamCMD needs you to log in manually to the isolated environment.")
        print("To log in and cache credentials for 'fpv_bot', run this command in your terminal:")
        print(f"\n    HOME={steamcmd_home} {STEAMCMD_PATH} +login fpv_bot\n")
        print("Enter your password and Steam Guard code when prompted, then type 'quit' to exit.")
        print("This only needs to be done once! Subsequent publishes will use the cached session.")
        print("="*80 + "\n")
        raise

def publish_track(track_id):
    """
    Full pipeline to stage and publish track to Steam Workshop.
    """
    # 1. Stage the files
    stage_files(track_id)
    
    # 2. Always create a new Workshop item (set to "0")
    published_file_id = "0"
    
    # 3. Generate VDF
    write_vdf(published_file_id, track_id)
    
    # 4. Upload
    stdout = run_steamcmd()
    
    # 5. Extract publishedfileid and display success
    # Search for "Created new item with ID <id>" or "ID <id>"
    match = re.search(r"Created new item with ID (\d+)", stdout)
    if not match:
        # Fallback regex
        match = re.search(r"ID\s+(\d+)", stdout)
        
    if match:
        new_id = match.group(1)
        print(f"\n[Publish] SUCCESS! Created new Steam Workshop item with ID: {new_id}")
        print(f"[Publish] Workshop URL: https://steamcommunity.com/sharedfiles/filedetails/?id={new_id}")
    else:
        print("\n[Publish] WARNING: Upload completed but could not parse the new Workshop Item ID from SteamCMD output.", file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 publish.py <track_id>")
        sys.exit(1)
    publish_track(sys.argv[1])
