import os
import uuid
import random
import shutil
from datetime import datetime

LIFTOFF_BASE_DIR = os.path.expanduser("~/.config/unity3d/LuGus Studios/Liftoff")
TRACKS_DIR = os.path.join(LIFTOFF_BASE_DIR, "Tracks")
RACES_DIR = os.path.join(LIFTOFF_BASE_DIR, "Races")

PROJECT_WORKSPACE = "/home/dev-user/Projects/procedural-fpv"
BACKUP_DIR = os.path.join(PROJECT_WORKSPACE, "backups")

def backup_existing_files(track_id):
    """
    Checks if files for track_id exist in the Liftoff directories and copies them
    to the project backups/ folder before they are modified.
    """
    track_source_dir = os.path.join(TRACKS_DIR, track_id)
    race_id = f"{track_id}_race"
    race_source_dir = os.path.join(RACES_DIR, race_id)
    
    # Check if anything exists to back up
    track_exists = os.path.isdir(track_source_dir)
    race_exists = os.path.isdir(race_source_dir)
    
    if not (track_exists or race_exists):
        return None  # Nothing to back up
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_backup_dir = os.path.join(BACKUP_DIR, f"{track_id}_{timestamp}")
    os.makedirs(dest_backup_dir, exist_ok=True)
    
    if track_exists:
        track_backup_path = os.path.join(dest_backup_dir, "Tracks")
        shutil.copytree(track_source_dir, track_backup_path)
        
    if race_exists:
        race_backup_path = os.path.join(dest_backup_dir, "Races")
        shutil.copytree(race_source_dir, race_backup_path)
        
    print(f"[Backup] Pre-write snapshot for '{track_id}' saved to: {dest_backup_dir}")
    return dest_backup_dir


def generate_track_xml(track_id, track_name, environment, blueprints):
    """
    Generates the XML string for a .track file.
    """
    # Create managed ID (random 9-digit string)
    managed_id = str(random.randint(100000000, 999999999))
    
    xml = []
    xml.append('<?xml version="1.0" encoding="utf-8"?>')
    xml.append('<Track xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
    xml.append('  <gameVersion>1.6.17</gameVersion>')
    xml.append('  <localID>')
    xml.append(f'    <str>{track_id}</str>')
    xml.append('    <version>1</version>')
    xml.append('    <type>TRACK</type>')
    xml.append('  </localID>')
    xml.append('  <managedID>')
    xml.append(f'    <str>{managed_id}</str>')
    xml.append('    <version>1</version>')
    xml.append('    <type>TRACK</type>')
    xml.append('  </managedID>')
    xml.append(f'  <name>{track_name}</name>')
    xml.append('  <description>Procedurally generated map by Antigravity</description>')
    xml.append('  <dependencies />')
    xml.append(f'  <environment>{environment}</environment>')
    xml.append('  <blueprints>')
    
    for bp in blueprints:
        x, y, z = bp['position']
        rx, ry, rz = bp['rotation']
        item_id = bp['item_id']
        inst_id = bp['instance_id']
        purpose = bp.get('purpose', 'Functional')
        
        xml.append('    <TrackBlueprint xsi:type="TrackBlueprintFlag">')
        xml.append(f'      <itemID>{item_id}</itemID>')
        xml.append(f'      <instanceID>{inst_id}</instanceID>')
        xml.append('      <position>')
        xml.append(f'        <x>{x:.6f}</x>')
        xml.append(f'        <y>{y:.6f}</y>')
        xml.append(f'        <z>{z:.6f}</z>')
        xml.append('      </position>')
        xml.append('      <rotation>')
        xml.append(f'        <x>{rx:.6f}</x>')
        xml.append(f'        <y>{ry:.6f}</y>')
        xml.append(f'        <z>{rz:.6f}</z>')
        xml.append('      </rotation>')
        xml.append(f'      <purpose>{purpose}</purpose>')
        xml.append('    </TrackBlueprint>')
        
    xml.append('  </blueprints>')
    max_instance_id = max(bp['instance_id'] for bp in blueprints) if blueprints else 0
    xml.append(f'  <lastTrackItemID>{max_instance_id}</lastTrackItemID>')
    xml.append('  <hideDefaultSpawnpoint>false</hideDefaultSpawnpoint>')
    xml.append('</Track>')
    
    return "\n".join(xml)

def generate_race_xml(track_id, race_id, race_name, checkpoint_ids, spawn_point_id, laps=3):
    """
    Generates the XML string for a .race file.
    """
    managed_id = str(random.randint(100000000, 999999999))
    
    # Generate list of GUIDs for the passages
    # Passages count is len(checkpoint_ids) + 1 because the finish gate is a pass through the start gate again.
    num_passages = len(checkpoint_ids) + 1
    passage_guids = [str(uuid.uuid4()) for _ in range(num_passages)]
    
    xml = []
    xml.append('<?xml version="1.0" encoding="utf-8"?>')
    xml.append('<Race xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
    xml.append('  <gameVersion>1.6.17</gameVersion>')
    xml.append('  <localID>')
    xml.append(f'    <str>{race_id}</str>')
    xml.append('    <version>1</version>')
    xml.append('    <type>RACE</type>')
    xml.append('  </localID>')
    xml.append('  <name>{0}</name>'.format(race_name))
    xml.append('  <description />')
    xml.append('  <dependencies>')
    xml.append('    <dependency>')
    xml.append(f'      <str>{track_id}</str>')
    xml.append('      <version>1</version>')
    xml.append('      <type>TRACK</type>')
    xml.append('    </dependency>')
    xml.append('  </dependencies>')
    xml.append(f'  <requiredLaps>{laps}</requiredLaps>')
    xml.append('  <validity>Valid</validity>')
    xml.append(f'  <spawnPointID>{spawn_point_id}</spawnPointID>')
    xml.append('  <checkPointPassages>')
    
    # Write each passage
    for i in range(num_passages):
        guid = passage_guids[i]
        
        if i == 0:
            # Start passage at the first checkpoint
            checkpoint_id = checkpoint_ids[0]
            passage_type = "Start"
            next_guid = passage_guids[1]
        elif i == num_passages - 1:
            # Finish passage back at the first checkpoint
            checkpoint_id = checkpoint_ids[0]
            passage_type = "Finish"
            next_guid = None
        else:
            # Intermediate checkpoints
            checkpoint_id = checkpoint_ids[i]
            passage_type = "Pass"
            next_guid = passage_guids[i + 1]
            
        xml.append('    <RaceCheckpointPassage>')
        xml.append(f'      <uniqueId>{guid}</uniqueId>')
        xml.append(f'      <checkPointID>{checkpoint_id}</checkPointID>')
        xml.append('      <checkPointSubID />')
        xml.append(f'      <passageType>{passage_type}</passageType>')
        xml.append('      <directionality>LeftToRight</directionality>')
        xml.append('      <nextPassageIDs>')
        if next_guid:
            xml.append(f'        <string>{next_guid}</string>')
        xml.append('      </nextPassageIDs>')
        xml.append('    </RaceCheckpointPassage>')
        
    xml.append('  </checkPointPassages>')
    xml.append('  <enableCompetitiveFeaturesWithGameMods>false</enableCompetitiveFeaturesWithGameMods>')
    xml.append('</Race>')
    
    return "\n".join(xml)

def save_track_and_race(track_id, display_name, environment, blueprints, checkpoint_ids, spawn_point_id, laps=3):
    """
    Compiles XMLs and writes them to Liftoff's Tracks and Races directories.
    """
    race_id = f"{track_id}_race"
    track_xml_content = generate_track_xml(track_id, display_name, environment, blueprints)
    race_xml_content = generate_race_xml(track_id, race_id, f"{display_name} Race", checkpoint_ids, spawn_point_id, laps)
    
    # Create backup snapshot of existing files if they exist
    backup_existing_files(track_id)
    
    # Determine target directories
    track_dest_dir = os.path.join(TRACKS_DIR, track_id)
    race_dest_dir = os.path.join(RACES_DIR, race_id)
    
    # Create directories if they do not exist
    os.makedirs(track_dest_dir, exist_ok=True)
    os.makedirs(race_dest_dir, exist_ok=True)
    
    # File paths
    track_filepath_versioned = os.path.join(track_dest_dir, f"{track_id}_0001.track")
    track_filepath_unversioned = os.path.join(track_dest_dir, f"{track_id}.track")
    # For newer versions of Liftoff, races in directories often use a suffix like _0001.race
    # Let's save it directly as {race_id}_0001.race to follow standard game serialization.
    race_filepath = os.path.join(race_dest_dir, f"{race_id}_0001.race")
    
    # Write .track files in UTF-8 (Liftoff game files are stored as UTF-8/ASCII despite xml header)
    with open(track_filepath_versioned, "w", encoding="utf-8") as f:
        f.write(track_xml_content)
    with open(track_filepath_unversioned, "w", encoding="utf-8") as f:
        f.write(track_xml_content)
        
    # Write .race file in UTF-8
    with open(race_filepath, "w", encoding="utf-8") as f:
        f.write(race_xml_content)
        
    return track_filepath_versioned, race_filepath
