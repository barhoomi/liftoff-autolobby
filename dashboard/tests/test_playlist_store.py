"""Tests for dashboard.control.playlist_store — playlists.json CRUD + trackcheck lint.

The reason this file exists at all is the failure the linter was written for: a typo'd
track or environment resolves to zero tracks at runtime and the bot silently falls back
to all_official_races. So the tests are mostly about *refusing* to save things.
"""

import json

import pytest

from dashboard.control import playlist_store as store


@pytest.fixture
def paths(project):
    return {"playlists_path": project["playlists_path"], "master_path": project["master_path"],
            "project_dir": project["root"]}


def upsert(paths, name, items, force=False):
    return store.upsert_playlist(name, items, force=force,
                                 playlists_path=paths["playlists_path"],
                                 master_path=paths["master_path"],
                                 project_dir=paths["project_dir"])


class TestLoadSave:
    def test_round_trip(self, paths):
        data = store.load_playlists(paths["playlists_path"])
        assert "all_official_races" in data
        data["extra"] = ["BC Track 0"]
        store.save_playlists(data, paths["playlists_path"])
        assert "extra" in store.load_playlists(paths["playlists_path"])

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert store.load_playlists(str(tmp_path / "nope.json")) == {}

    def test_save_is_atomic_and_leaves_no_temp_file(self, paths, project):
        import os

        store.save_playlists({"a": []}, paths["playlists_path"])
        leftovers = [n for n in os.listdir(project["config_dir"]) if ".tmp." in n]
        assert leftovers == []

    def test_saved_json_is_reparseable_and_pretty(self, paths):
        store.save_playlists({"a": [{"environment": "*", "track": "*", "mode": "Classic Race"}]},
                             paths["playlists_path"])
        text = open(paths["playlists_path"]).read()
        assert text.endswith("\n")
        assert json.loads(text) == {"a": [{"environment": "*", "track": "*", "mode": "Classic Race"}]}

    def test_non_object_json_is_rejected(self, tmp_path):
        path = tmp_path / "playlists.json"
        path.write_text("[1, 2]")
        with pytest.raises(store.PlaylistStoreError):
            store.load_playlists(str(path))


class TestValidation:
    def _master(self, paths):
        return store.load_master(paths["master_path"])

    def test_clean_playlist_has_no_findings(self, paths):
        findings = store.validate_playlist(
            "ok", [{"environment": "Bando City", "track": "*", "mode": "Classic Race"}],
            self._master(paths))
        assert findings == []

    def test_unknown_environment_blocks(self, paths):
        findings = store.validate_playlist(
            "typo", [{"environment": "Bandoo City", "track": "*", "mode": "Classic Race"}],
            self._master(paths))
        assert store.blocking(findings)
        assert findings[0]["code"] == "UNKNOWN_ENVIRONMENT"

    def test_unknown_mode_blocks(self, paths):
        findings = store.validate_playlist(
            "typo", [{"environment": "Bando City", "track": "*", "mode": "Rice"}],
            self._master(paths))
        assert {f["code"] for f in store.blocking(findings)} == {"UNKNOWN_MODE"}

    def test_malformed_entry_blocks(self, paths):
        findings = store.validate_playlist("bad", [42], self._master(paths))
        assert {f["code"] for f in store.blocking(findings)} == {"INVALID_ENTRY_SHAPE"}

    def test_non_list_playlist_blocks(self, paths):
        findings = store.validate_playlist("bad", "not a list", self._master(paths))
        assert findings[0]["code"] == "INVALID_PLAYLIST_SHAPE"

    def test_track_typo_is_a_warning_not_a_block(self, paths):
        findings = store.validate_playlist(
            "typo", [{"environment": "Bando City", "track": "BC Trakc 0", "mode": "Classic Race"}],
            self._master(paths))
        assert not store.blocking(findings)
        assert {f["code"] for f in findings} == {"ENTRY_NO_MATCHES", "EMPTY_RESOLUTION"}

    def test_without_a_master_list_only_shape_checks_run(self, paths):
        # master_tracks_list.json is generated from a live install and is routinely
        # absent; every entry would "match nothing" against an empty catalogue, which is
        # noise rather than information.
        findings = store.validate_playlist(
            "x", [{"environment": "Bando City", "track": "anything", "mode": "Classic Race"}], None)
        assert findings == []
        blocking_findings = store.validate_playlist(
            "x", [{"environment": "Nowhere", "track": "*", "mode": "Classic Race"}], None)
        assert store.blocking(blocking_findings)


class TestUpsert:
    def test_saves_a_clean_playlist(self, paths):
        data, findings = upsert(paths, "new_one",
                                [{"environment": "The Green", "track": "*", "mode": "Classic Race"}])
        assert "new_one" in data
        assert findings == []
        assert "new_one" in store.load_playlists(paths["playlists_path"])

    def test_refuses_a_blocking_playlist_even_with_force(self, paths):
        with pytest.raises(store.PlaylistValidationError) as exc:
            upsert(paths, "bad", [{"environment": "Nowhere", "track": "*", "mode": "Classic Race"}],
                   force=True)
        assert exc.value.findings[0]["code"] == "UNKNOWN_ENVIRONMENT"
        assert "bad" not in store.load_playlists(paths["playlists_path"])

    def test_refuses_a_warning_playlist_without_force(self, paths):
        with pytest.raises(store.PlaylistValidationError):
            upsert(paths, "empty", [{"environment": "Bando City", "track": "nope", "mode": "Classic Race"}])
        assert "empty" not in store.load_playlists(paths["playlists_path"])

    def test_force_saves_a_warning_playlist(self, paths):
        # Legitimate case: the workshop track is not installed on this machine yet.
        data, findings = upsert(paths, "future",
                                [{"environment": "Bando City", "track": "nope", "mode": "Classic Race"}],
                                force=True)
        assert "future" in data
        assert store.warnings(findings)

    def test_replacing_an_existing_playlist_keeps_the_others(self, paths):
        upsert(paths, "bando_only", [{"environment": "The Green", "track": "*", "mode": "Classic Race"}])
        data = store.load_playlists(paths["playlists_path"])
        assert "all_official_races" in data
        assert data["bando_only"][0]["environment"] == "The Green"

    @pytest.mark.parametrize("name", ["", "  ", " leading", "trailing ", "a/b", None, 5])
    def test_rejects_unusable_names(self, paths, name):
        # The name is written verbatim into playlist_name.txt and compared by the
        # orchestrator's watcher, so whitespace/slashes are not cosmetic problems.
        with pytest.raises(store.PlaylistStoreError):
            upsert(paths, name, [])


class TestDelete:
    def test_deletes_a_normal_playlist(self, paths):
        store.delete_playlist("bando_only", playlists_path=paths["playlists_path"])
        assert "bando_only" not in store.load_playlists(paths["playlists_path"])

    def test_refuses_to_delete_the_fallback_playlist(self, paths):
        with pytest.raises(store.PlaylistStoreError) as exc:
            store.delete_playlist("all_official_races", playlists_path=paths["playlists_path"])
        assert "fallback" in str(exc.value)

    def test_refuses_to_delete_the_active_playlist(self, paths):
        with pytest.raises(store.PlaylistStoreError):
            store.delete_playlist("bando_only", active_playlist="bando_only",
                                  playlists_path=paths["playlists_path"])

    def test_unknown_playlist_is_an_error(self, paths):
        with pytest.raises(store.PlaylistStoreError):
            store.delete_playlist("nope", playlists_path=paths["playlists_path"])


class TestLintAll:
    def test_reports_per_playlist_findings_and_master_availability(self, paths):
        data, findings, master_available = store.lint_all(
            paths["playlists_path"], paths["master_path"], paths["project_dir"])
        assert set(data) == {"all_official_races", "bando_only"}
        assert findings["all_official_races"] == []
        assert master_available is True

    def test_master_absence_is_reported(self, paths, project):
        import os

        os.remove(project["master_path"])
        _, _, master_available = store.lint_all(paths["playlists_path"], paths["master_path"],
                                                paths["project_dir"])
        assert master_available is False
