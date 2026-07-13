import os
import re
import uuid
import xml.etree.ElementTree as ET

import pytest

import src.io as io_mod
from src.assets import GATE_OCTAGON, SPAWN_SINGLE
from src.io import backup_existing_files, generate_race_xml, generate_track_xml, save_track_and_race

XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"


def _sample_blueprints():
    return [
        {"position": (1.0, 2.5, 3.0), "rotation": (0.0, 90.0, 0.0),
         "item_id": GATE_OCTAGON, "instance_id": 1, "purpose": "Functional"},
        {"position": (-4.25, 5.0, -6.0), "rotation": (10.0, 180.0, 0.0),
         "item_id": GATE_OCTAGON, "instance_id": 2},  # no "purpose" key on purpose
        {"position": (0.0, 1.0, 0.0), "rotation": (0.0, 0.0, 0.0),
         "item_id": SPAWN_SINGLE, "instance_id": 3, "purpose": "Functional"},
    ]


def _track_root(blueprints=None):
    if blueprints is None:
        blueprints = _sample_blueprints()
    return ET.fromstring(generate_track_xml("my_track", "My Track", "TheDrawingBoard", blueprints))


class TestGenerateTrackXml:
    def test_output_is_well_formed_xml_with_track_root(self):
        root = _track_root()
        assert root.tag == "Track"

    def test_local_id_fields(self):
        root = _track_root()
        assert root.find("localID/str").text == "my_track"
        assert root.find("localID/type").text == "TRACK"
        assert root.find("localID/version").text == "1"

    def test_managed_id_is_a_9_digit_string(self):
        root = _track_root()
        assert re.fullmatch(r"\d{9}", root.find("managedID/str").text)

    def test_name_and_environment(self):
        root = _track_root()
        assert root.find("name").text == "My Track"
        assert root.find("environment").text == "TheDrawingBoard"

    def test_one_blueprint_element_per_input_with_matching_ids(self):
        blueprints = _sample_blueprints()
        root = _track_root(blueprints)
        elems = root.findall("blueprints/TrackBlueprint")
        assert len(elems) == len(blueprints)
        for bp, elem in zip(blueprints, elems):
            assert elem.find("itemID").text == bp["item_id"]
            assert int(elem.find("instanceID").text) == bp["instance_id"]
            assert elem.get(XSI_TYPE) == "TrackBlueprintFlag"

    def test_positions_written_with_6_decimal_places(self):
        root = _track_root()
        first = root.find("blueprints/TrackBlueprint")
        assert first.find("position/x").text == "1.000000"
        assert first.find("position/y").text == "2.500000"
        assert first.find("position/z").text == "3.000000"

    def test_purpose_defaults_to_functional_when_absent(self):
        root = _track_root()
        purposes = [e.find("purpose").text for e in root.findall("blueprints/TrackBlueprint")]
        assert purposes == ["Functional", "Functional", "Functional"]

    def test_last_track_item_id_is_max_instance_id(self):
        root = _track_root()
        assert root.find("lastTrackItemID").text == "3"

    def test_empty_blueprint_list_yields_zero_last_item_id_and_parses(self):
        root = _track_root(blueprints=[])
        assert root.find("lastTrackItemID").text == "0"
        assert root.findall("blueprints/TrackBlueprint") == []

    def test_round_trips_through_trackcheck_parser(self, tmp_path):
        # The generator's writer and trackcheck's parser are the two halves of one
        # contract -- what we write must come back out of the shared parser intact.
        from trackcheck.parser import parse_track_blueprints, parse_track_file

        blueprints = _sample_blueprints()
        path = tmp_path / "roundtrip.track"
        path.write_text(generate_track_xml("rt_id", "RT Name", "TheGreen", blueprints),
                        encoding="utf-8")

        assert parse_track_file(str(path)) == ("rt_id", "RT Name", "TheGreen")
        parsed = parse_track_blueprints(str(path))
        assert [bp["instance_id"] for bp in parsed] == [1, 2, 3]
        assert parsed[0]["position"] == (1.0, 2.5, 3.0)
        assert parsed[0]["item_id"] == GATE_OCTAGON


def _race_root(checkpoint_ids=(10, 11, 12), spawn_point_id=99, laps=3):
    return ET.fromstring(generate_race_xml(
        "my_track", "my_track_race_1", "My Race", list(checkpoint_ids), spawn_point_id, laps=laps))


class TestGenerateRaceXml:
    def test_output_is_well_formed_xml_with_race_root(self):
        root = _race_root()
        assert root.tag == "Race"

    def test_local_id_and_name(self):
        root = _race_root()
        assert root.find("localID/str").text == "my_track_race_1"
        assert root.find("localID/type").text == "RACE"
        assert root.find("name").text == "My Race"

    def test_track_dependency_points_back_at_track_id(self):
        root = _race_root()
        dep = root.find("dependencies/dependency")
        assert dep.find("str").text == "my_track"
        assert dep.find("type").text == "TRACK"

    def test_required_laps_default_and_override(self):
        assert _race_root().find("requiredLaps").text == "3"
        assert _race_root(laps=5).find("requiredLaps").text == "5"

    def test_spawn_point_id(self):
        root = _race_root(spawn_point_id=1234)
        assert root.find("spawnPointID").text == "1234"

    def test_passage_count_is_checkpoints_plus_one(self):
        # Finish is a second pass through the start gate, hence the +1.
        root = _race_root(checkpoint_ids=(10, 11, 12))
        passages = root.findall("checkPointPassages/RaceCheckpointPassage")
        assert len(passages) == 4

    def test_passage_types_are_start_pass_finish_in_order(self):
        root = _race_root(checkpoint_ids=(10, 11, 12))
        types = [p.find("passageType").text
                 for p in root.findall("checkPointPassages/RaceCheckpointPassage")]
        assert types == ["Start", "Pass", "Pass", "Finish"]

    def test_start_and_finish_both_use_the_first_checkpoint(self):
        root = _race_root(checkpoint_ids=(10, 11, 12))
        cps = [int(p.find("checkPointID").text)
               for p in root.findall("checkPointPassages/RaceCheckpointPassage")]
        assert cps == [10, 11, 12, 10]

    def test_next_passage_guids_form_a_single_linked_chain(self):
        root = _race_root(checkpoint_ids=(10, 11, 12))
        passages = root.findall("checkPointPassages/RaceCheckpointPassage")
        for current, following in zip(passages, passages[1:]):
            assert current.find("nextPassageIDs/string").text == following.find("uniqueId").text
        # Finish passage terminates the chain: no next id at all.
        assert passages[-1].find("nextPassageIDs/string") is None

    def test_unique_ids_are_distinct_valid_uuids(self):
        root = _race_root(checkpoint_ids=(10, 11, 12))
        ids = [p.find("uniqueId").text
               for p in root.findall("checkPointPassages/RaceCheckpointPassage")]
        assert len(set(ids)) == len(ids)
        for value in ids:
            uuid.UUID(value)  # raises if not a valid UUID

    def test_single_checkpoint_race_is_start_then_finish(self):
        root = _race_root(checkpoint_ids=(7,))
        passages = root.findall("checkPointPassages/RaceCheckpointPassage")
        assert [p.find("passageType").text for p in passages] == ["Start", "Finish"]
        assert [int(p.find("checkPointID").text) for p in passages] == [7, 7]

    def test_round_trips_through_trackcheck_parser(self, tmp_path):
        from trackcheck.parser import parse_race_checkpoints, parse_race_file

        path = tmp_path / "roundtrip.race"
        path.write_text(generate_race_xml("rt_track", "rt_race", "RT Race", [5, 6, 7], 42),
                        encoding="utf-8")

        assert parse_race_file(str(path)) == ("RT Race", "rt_track")
        info = parse_race_checkpoints(str(path))
        assert info["spawn_point_id"] == 42
        assert info["checkpoint_sequence"] == [5, 6, 7, 5]


@pytest.fixture
def io_dirs(tmp_path, monkeypatch):
    """Redirect the module-level Liftoff/backup directories into tmp_path so the
    file-op helpers can be exercised without touching the real game install."""
    tracks = tmp_path / "Tracks"
    races = tmp_path / "Races"
    backups = tmp_path / "backups"
    monkeypatch.setattr(io_mod, "TRACKS_DIR", str(tracks))
    monkeypatch.setattr(io_mod, "RACES_DIR", str(races))
    monkeypatch.setattr(io_mod, "BACKUP_DIR", str(backups))
    return tracks, races, backups


class TestBackupExistingFiles:
    def test_nothing_to_back_up_returns_none(self, io_dirs):
        tracks, races, backups = io_dirs
        assert backup_existing_files("ghost_track") is None
        assert not backups.exists()

    def test_existing_track_dir_is_snapshotted(self, io_dirs):
        tracks, races, backups = io_dirs
        src = tracks / "t1"
        src.mkdir(parents=True)
        (src / "t1.track").write_text("<Track/>", encoding="utf-8")

        dest = backup_existing_files("t1")
        assert dest is not None
        assert os.path.exists(os.path.join(dest, "Tracks", "t1.track"))
        # Original untouched.
        assert (src / "t1.track").exists()

    def test_existing_race_dirs_matching_prefix_are_snapshotted(self, io_dirs):
        tracks, races, backups = io_dirs
        race_dir = races / "t1_race_0101"
        race_dir.mkdir(parents=True)
        (race_dir / "t1_race_0101_0001.race").write_text("<Race/>", encoding="utf-8")
        # A different track's race must not be swept into t1's backup.
        other = races / "t2_race_0101"
        other.mkdir()
        (other / "t2.race").write_text("<Race/>", encoding="utf-8")

        dest = backup_existing_files("t1")
        assert dest is not None
        assert os.path.exists(os.path.join(dest, "Races", "t1_race_0101", "t1_race_0101_0001.race"))
        assert not os.path.exists(os.path.join(dest, "Races", "t2_race_0101"))


class TestSaveTrackAndRace:
    def _save(self):
        blueprints = _sample_blueprints()
        return save_track_and_race("t1", "Track One", "TheDrawingBoard",
                                   blueprints, checkpoint_ids=[1, 2], spawn_point_id=3)

    def test_writes_versioned_and_unversioned_track_files_with_same_content(self, io_dirs):
        tracks, races, backups = io_dirs
        track_path, race_path = self._save()
        versioned = tracks / "t1" / "t1_0001.track"
        unversioned = tracks / "t1" / "t1.track"
        assert track_path == str(versioned)
        assert versioned.read_text(encoding="utf-8") == unversioned.read_text(encoding="utf-8")

    def test_race_file_written_under_generated_race_id_dir(self, io_dirs):
        tracks, races, backups = io_dirs
        _, race_path = self._save()
        assert os.path.exists(race_path)
        assert race_path.endswith("_0001.race")
        assert os.path.basename(os.path.dirname(race_path)).startswith("t1_race_")

    def test_stale_race_dirs_for_same_track_are_removed(self, io_dirs):
        tracks, races, backups = io_dirs
        stale = races / "t1_race_old"
        stale.mkdir(parents=True)
        (stale / "old.race").write_text("<Race/>", encoding="utf-8")
        other = races / "t2_race_old"
        other.mkdir()

        self._save()
        assert not stale.exists()      # replaced
        assert other.exists()          # other tracks' races untouched
