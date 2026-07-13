"""Unit tests for trackcheck.playlist_match, plus a parity test proving its
resolution semantics are identical to orchestrator/run_headless_lobby.py's
resolve_and_write_playlist() on the same fixture data (see playlist_match.py's
module docstring for why this is a parity test rather than a shared import)."""

import json
import os

import pytest

import run_headless_lobby  # available via pytest.ini's `pythonpath = . generator orchestrator`

from trackcheck.playlist_match import (
    is_match,
    normalize_playlist_item,
    resolve_playlist,
    resolve_playlist_item,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class TestIsMatch:
    def test_exact_match_case_insensitive(self):
        assert is_match("01 - Par For The Course", "01 - PAR FOR THE COURSE")

    def test_wildcard_match(self):
        assert is_match("*Honk*", "[Honk] Backtrack")

    def test_no_match(self):
        assert not is_match("*Honk*", "01 - Par For The Course")

    def test_whitespace_is_stripped(self):
        assert is_match("  01 - Par For The Course  ", "01 - Par For The Course")


class TestNormalizePlaylistItem:
    def test_bare_string_defaults_to_wildcard_env_and_infinite_race(self):
        assert normalize_playlist_item("01 - Par For The Course") == ("*", "01 - Par For The Course", "Infinite Race")

    def test_dict_item_uses_its_own_fields(self):
        item = {"environment": "The Green", "track": "*", "mode": "Classic Race"}
        assert normalize_playlist_item(item) == ("The Green", "*", "Classic Race")

    def test_dict_item_defaults_missing_fields(self):
        assert normalize_playlist_item({}) == ("*", "*", "Infinite Race")

    def test_invalid_shape_returns_none(self):
        assert normalize_playlist_item(123) is None
        assert normalize_playlist_item(None) is None


class TestResolvePlaylistItem:
    def setup_method(self):
        self.master = load_fixture("master_for_lint.json")

    def test_wildcard_env_and_wildcard_track_matches_official_and_workshop_only(self):
        matches = resolve_playlist_item("*", "*", self.master)
        track_names = {t for t, _ in matches}
        assert "My Local Test Track" not in track_names  # local is excluded
        assert "01 - The Biggest Yet" in track_names
        assert "[Honk] Backtrack" in track_names

    def test_specific_environment_pattern(self):
        matches = resolve_playlist_item("The Green", "*", self.master)
        envs = {e for _, e in matches}
        assert envs == {"The Green"}

    def test_environment_pattern_normalizes_variant_spelling(self):
        matches = resolve_playlist_item("thedrawingboard", "*", self.master)
        envs = {e for _, e in matches}
        assert envs == {"The Drawing Board"}

    def test_track_wildcard_pattern(self):
        matches = resolve_playlist_item("The Drawing Board", "*Honk*", self.master)
        track_names = {t for t, _ in matches}
        assert track_names == {"[Honk] Backtrack", "[Honk] Canyon Race v3 3 Laps"}

    def test_typo_matches_nothing(self):
        matches = resolve_playlist_item("The Green", "01 - Par For Teh Course", self.master)
        assert matches == []


class TestResolvePlaylist:
    def setup_method(self):
        self.master = load_fixture("master_for_lint.json")

    def test_dedupes_resolved_tuples_in_first_seen_order(self):
        items = ["01 - Par For The Course", "01 - Par For The Course"]
        resolved, per_entry = resolve_playlist(items, self.master)
        assert resolved == [("01 - Par For The Course", "The Green", "Infinite Race")]
        assert per_entry[0]["match_count"] == 1
        assert per_entry[1]["match_count"] == 1  # still reported as a match even though deduped

    def test_per_entry_match_count_zero_for_typo(self):
        items = [{"environment": "The Green", "track": "01 - Par For Teh Course", "mode": "Infinite Race"}]
        resolved, per_entry = resolve_playlist(items, self.master)
        assert resolved == []
        assert per_entry[0]["match_count"] == 0

    def test_invalid_entry_shape_is_reported_but_does_not_crash(self):
        resolved, per_entry = resolve_playlist([123], self.master)
        assert resolved == []
        assert per_entry[0]["valid_shape"] is False


class TestParityWithOrchestratorResolver:
    """resolve_and_write_playlist() in orchestrator/run_headless_lobby.py computes
    project_dir from its own __file__ at call time, so pointing it at a throwaway
    fixture repo layout is just a matter of monkeypatching that one attribute --
    no need to touch the real playlists.json/master_tracks_list.json, and no risk to
    the live bot (main() only runs under `if __name__ == "__main__"`, never imported)."""

    def test_resolution_matches_trackcheck_exactly(self, tmp_path, monkeypatch):
        playlists_data = {
            "parity_playlist": [
                {"environment": "The Drawing Board", "track": "*Honk*", "mode": "Infinite Race"},
                "01 - Par For The Course",
                {"environment": "The Green", "track": "03 - Club House", "mode": "Classic Race"},
            ],
        }
        master_data = load_fixture("master_for_lint.json")

        fake_repo = tmp_path / "fake_repo"
        fake_orchestrator_dir = fake_repo / "orchestrator"
        fake_orchestrator_dir.mkdir(parents=True)
        (fake_repo / "playlists.json").write_text(json.dumps(playlists_data))
        (fake_repo / "master_tracks_list.json").write_text(json.dumps(master_data))

        # resolve_and_write_playlist() derives project_dir from
        # os.path.dirname(os.path.dirname(os.path.abspath(__file__))) at call time --
        # repointing __file__ at our fake orchestrator dir redirects it to the fixture
        # files above without touching the real repo-root playlists.json.
        monkeypatch.setattr(run_headless_lobby, "__file__", str(fake_orchestrator_dir / "run_headless_lobby.py"))

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        output_file = plugins_dir / "tracks_to_rotate.txt"

        run_headless_lobby.resolve_and_write_playlist(
            "parity_playlist", shuffle_enabled=False, output_file=str(output_file),
        )

        written_lines = [
            line for line in output_file.read_text().splitlines() if line and not line.startswith("#")
        ]
        written_tuples = [tuple(line.split(",")) for line in written_lines]

        resolved, _ = resolve_playlist(playlists_data["parity_playlist"], master_data)

        assert written_tuples == resolved
        assert written_tuples  # sanity: the fixture actually resolved to something
