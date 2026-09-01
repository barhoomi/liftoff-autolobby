"""Tests for orchestrator/workshop_items.py -- where workshop items live, and quarantine.

Quarantine is the safety net the whole workshop pipeline leans on (both the in-game
download and, later, the steamcmd install), so the properties asserted here are the ones
an operator's data depends on: the files are MOVED, never deleted; the reason survives
next to them; and asking to quarantine something that isn't there is an error rather than
a silent success.
"""

import json
import os
import shutil

import pytest

from workshop_items import (
    LIFTOFF_APP_ID,
    QUARANTINE_MANIFEST,
    quarantine_item,
    quarantine_root,
    workshop_content_root,
    workshop_content_roots,
    workshop_item_dir,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "trackcheck", "tests", "fixtures")


class RecordingLogger:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))
        return fields

    def decision(self, kind, detail):
        return self.emit("decision", kind=kind, detail=detail)

    def error(self, message, context=None, playlist=None):
        return self.emit("error", message=message, context=context)


class TestContentRoots:
    def test_env_override_wins(self, tmp_path):
        root = tmp_path / "content" / str(LIFTOFF_APP_ID)
        root.mkdir(parents=True)
        env = {"FPV_WORKSHOP_CONTENT_DIR": str(root)}
        assert workshop_content_roots(env=env)[0] == str(root)
        assert workshop_content_root(env=env) == str(root)

    def test_historical_candidate_order_is_unchanged(self):
        """gather_tracks.py used to inline these three, in this order. The refactor that
        moved them here must not change which directory an existing host resolves."""
        roots = workshop_content_roots(env={})
        assert roots[0].endswith(
            "/.steam/debian-installation/steamapps/workshop/content/{}".format(LIFTOFF_APP_ID))
        assert roots[1].endswith("/.steam/steam/steamapps/workshop/content/{}".format(LIFTOFF_APP_ID))
        assert roots[2].endswith("/.local/share/Steam/steamapps/workshop/content/{}".format(LIFTOFF_APP_ID))

    def test_game_dir_candidate_is_appended_not_prepended(self, tmp_path):
        game_dir = tmp_path / "steamapps" / "common" / "Liftoff"
        game_dir.mkdir(parents=True)
        roots = workshop_content_roots(game_dir=str(game_dir), env={})
        assert roots[-1] == str(tmp_path / "steamapps" / "workshop" / "content" / str(LIFTOFF_APP_ID))
        assert len(roots) == 4

    def test_content_root_is_none_when_nothing_exists(self, tmp_path):
        assert workshop_content_root(env={"FPV_WORKSHOP_CONTENT_DIR": str(tmp_path / "nope")}) is None

    def test_item_dir_resolves_only_when_the_directory_exists(self, tmp_path):
        root = tmp_path / "content"
        (root / "123").mkdir(parents=True)
        env = {"FPV_WORKSHOP_CONTENT_DIR": str(root)}
        assert workshop_item_dir("123", env=env) == str(root / "123")
        assert workshop_item_dir(" 123 ", env=env) == str(root / "123")
        assert workshop_item_dir("456", env=env) is None


class TestQuarantineRoot:
    def test_env_override(self, tmp_path):
        assert quarantine_root(env={"FPV_QUARANTINE_DIR": str(tmp_path)}) == str(tmp_path)

    def test_defaults_next_to_the_repo(self, tmp_path):
        assert quarantine_root(project_dir=str(tmp_path), env={}) == str(tmp_path / "quarantine")


class TestQuarantineItem:
    @pytest.fixture
    def item(self, tmp_path):
        dest = tmp_path / "content" / "9001"
        shutil.copytree(os.path.join(FIXTURES, "no_gates_item"), str(dest))
        return dest

    def test_files_are_moved_not_deleted(self, item, tmp_path):
        manifest = quarantine_item(str(item), ["GATE_DATA_MISSING"], "ingame_download",
                                   env={"FPV_QUARANTINE_DIR": str(tmp_path / "q")})
        assert not os.path.exists(str(item))
        moved = manifest["quarantine_path"]
        assert os.path.isdir(moved)
        # the actual game files came along, not just an empty marker directory
        assert any(name.endswith(".track") for name in os.listdir(moved))

    def test_destination_is_namespaced_by_source_and_stamped(self, item, tmp_path):
        manifest = quarantine_item(str(item), ["GATE_DATA_MISSING"], "ingame_download",
                                   env={"FPV_QUARANTINE_DIR": str(tmp_path / "q")})
        rel = os.path.relpath(manifest["quarantine_path"], str(tmp_path / "q"))
        source, leaf = rel.split(os.sep)
        assert source == "ingame_download"
        assert leaf.startswith("9001-")  # <item>-<UTC stamp>, so two rejections can coexist

    def test_manifest_records_the_reasons_beside_the_files(self, item, tmp_path):
        manifest = quarantine_item(str(item), ["GATE_DATA_MISSING", "SPAWN_DATA_MISSING"],
                                   "ingame_download", published_id="9001",
                                   env={"FPV_QUARANTINE_DIR": str(tmp_path / "q")})
        with open(os.path.join(manifest["quarantine_path"], QUARANTINE_MANIFEST)) as fh:
            written = json.load(fh)
        assert written["reasons"] == ["GATE_DATA_MISSING", "SPAWN_DATA_MISSING"]
        assert written["published_id"] == "9001"
        assert written["source"] == "ingame_download"
        assert written["original_path"] == os.path.abspath(str(item))

    def test_emits_one_quarantine_event_with_the_reasons(self, item, tmp_path):
        logger = RecordingLogger()
        quarantine_item(str(item), ["GATE_DATA_MISSING"], "ingame_download", logger=logger,
                        env={"FPV_QUARANTINE_DIR": str(tmp_path / "q")})
        assert [e for e, _ in logger.events] == ["quarantine"]
        _, fields = logger.events[0]
        assert fields["reasons"] == ["GATE_DATA_MISSING"]
        assert fields["source"] == "ingame_download"
        assert fields["item"] == "9001"

    def test_two_rejections_of_the_same_id_do_not_collide(self, tmp_path):
        from datetime import datetime, timezone
        qdir = {"FPV_QUARANTINE_DIR": str(tmp_path / "q")}
        paths = []
        for stamp in (datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
                      datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)):
            dest = tmp_path / "content" / "9001"
            shutil.copytree(os.path.join(FIXTURES, "no_gates_item"), str(dest))
            paths.append(quarantine_item(str(dest), ["GATE_DATA_MISSING"], "ingame_download",
                                         env=qdir, now=stamp)["quarantine_path"])
        assert paths[0] != paths[1]
        assert all(os.path.isdir(p) for p in paths)

    def test_missing_directory_raises_rather_than_silently_passing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            quarantine_item(str(tmp_path / "nope"), ["X"], "ingame_download",
                            env={"FPV_QUARANTINE_DIR": str(tmp_path / "q")})
