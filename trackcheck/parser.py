"""Layer 1 (correctness) — the one robust XML parser for Liftoff .track/.race files.

Extracted verbatim from `orchestrator/gather_tracks.py` (encoding fallback, XML
declaration stripping, localID/name/environment extraction, race TRACK dependency
extraction) so there is exactly one parser implementation (CLAUDE.md rule 4: single
source of truth). `gather_tracks.py` now imports `ENV_MAPPING`, `normalize_env`,
`parse_xml_robust`, `parse_track_file`, `parse_race_file` from here instead of
defining them itself.

Two new functions were added for the validate/quality layers, which need more than
just (localID, name, environment) / (name, track_dep):

- `parse_track_blueprints(filepath)` — full TrackBlueprint list (item id, instance id,
  position, purpose), needed to check gate/spawn presence and to build track geometry.
- `parse_race_checkpoints(filepath)` — spawnPointID and the ordered checkPointID
  sequence from checkPointPassages, needed to build the racing-order gate sequence
  (blueprint declaration order in a .track file is not guaranteed to be lap order —
  the .race file's checkpoint passage order is the ground truth).
"""

import re
import xml.etree.ElementTree as ET

# Normalization map for Liftoff environment names. Also used by validate_item() to
# decide whether a track's declared environment is a known/supported one: an
# environment is "supported" iff its raw string is a *key* here (not just whatever
# normalize_env()'s CamelCase-split fallback happens to produce for typos).
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


def normalize_env(env):
    if not env:
        return "Unknown"
    normalized = ENV_MAPPING.get(env)
    if normalized:
        return normalized
    # Fallback formatting: CamelCase to space-separated words
    spaced = re.sub(r'(?<!^)(?=[A-Z])', ' ', env)
    return spaced


def is_known_environment(env):
    """True iff `env` (the raw, un-normalized string straight out of the XML) is a
    recognized key in ENV_MAPPING. Distinct from normalize_env(), which always
    returns *something* (a CamelCase-split guess) even for names it doesn't know —
    validate_item() needs to tell "known Liftoff environment" apart from "typo/new
    environment we've never seen", and normalize_env()'s fallback can't do that.
    """
    if not env:
        return False
    return env in ENV_MAPPING


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


def _parse_float(elem, tag):
    child = elem.find(tag)
    if child is None or child.text is None:
        return 0.0
    try:
        return float(child.text.strip())
    except ValueError:
        return 0.0


def parse_track_blueprints(filepath):
    """Full TrackBlueprint list from a .track file: item id, instance id, position
    (x, y, z), purpose. Returns None on any parse failure (mirrors parse_track_file's
    fail-soft contract so callers can treat every parse_* function uniformly).
    Needed by validate_item() (gate/spawn presence) and geometry.py (quality metrics).
    """
    try:
        root = parse_xml_robust(filepath)
        blueprints = []
        for bp in root.findall('.//TrackBlueprint'):
            item_id_elem = bp.find('itemID')
            item_id = item_id_elem.text.strip() if item_id_elem is not None and item_id_elem.text else None

            instance_id_elem = bp.find('instanceID')
            if instance_id_elem is None or instance_id_elem.text is None:
                continue
            try:
                instance_id = int(instance_id_elem.text.strip())
            except ValueError:
                continue

            pos_elem = bp.find('position')
            if pos_elem is not None:
                position = (_parse_float(pos_elem, 'x'), _parse_float(pos_elem, 'y'), _parse_float(pos_elem, 'z'))
            else:
                position = (0.0, 0.0, 0.0)

            purpose_elem = bp.find('purpose')
            purpose = purpose_elem.text.strip() if purpose_elem is not None and purpose_elem.text else None

            blueprints.append({
                'item_id': item_id,
                'instance_id': instance_id,
                'position': position,
                'purpose': purpose,
            })
        return blueprints
    except Exception:
        return None


def parse_race_checkpoints(filepath):
    """spawnPointID and the ordered checkPointID sequence (Start, Pass..., Finish)
    from a .race file's checkPointPassages. This is the racing-order ground truth —
    a .track file's TrackBlueprint declaration order is not guaranteed to match lap
    order. Returns None on any parse failure.

    Returned dict: {"spawn_point_id": int|None, "checkpoint_sequence": [int, ...]}
    The sequence is exactly as declared (including the repeated first checkpoint at
    the Finish passage, per generator/src/io.py's convention of closing the lap back
    through the start gate) — callers that want a deduplicated "unique ordered gates"
    list should drop a trailing repeat of the first id themselves (geometry.py does).
    """
    try:
        root = parse_xml_robust(filepath)

        spawn_elem = root.find('spawnPointID')
        spawn_point_id = None
        if spawn_elem is not None and spawn_elem.text:
            try:
                spawn_point_id = int(spawn_elem.text.strip())
            except ValueError:
                spawn_point_id = None

        checkpoint_sequence = []
        # Iterate passages in document order: Start, Pass..., Finish is the
        # convention this codebase's own writer (generator/src/io.py) uses, and
        # XML element order round-trips as document order under ElementTree.
        for passage in root.findall('.//RaceCheckpointPassage'):
            cp_elem = passage.find('checkPointID')
            if cp_elem is None or cp_elem.text is None:
                continue
            try:
                checkpoint_sequence.append(int(cp_elem.text.strip()))
            except ValueError:
                continue

        return {
            'spawn_point_id': spawn_point_id,
            'checkpoint_sequence': checkpoint_sequence,
        }
    except Exception:
        return None
