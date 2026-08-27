"""Unit tests for trackcheck.playlist_match, plus a parity test proving the runtime
resolver (dashboard.control.playlists.resolve_and_write_playlist) writes exactly what
this module resolves, on the same fixture data.

The parity test predates bot-dashboard.md's D5 extraction, when the resolver carried its
own copy of the matching rules and this module was a port of them. The resolver now
*imports* this module (see its docstring), so the test no longer guards a duplicate --
it guards the writer: definition order, dedup, and the exact CSV shape the plugin parses
out of tracks_to_rotate.txt."""

import json
import os

import pytest

from dashboard.control import playlists as control_playlists

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


class TestParityWithRuntimeResolver:
    """resolve_and_write_playlist() takes its two catalog paths as optional arguments, so
    it can be pointed at a throwaway fixture pair -- no need to touch the real
    playlists.json/master_tracks_list.json, and no risk to the live bot."""

    def test_resolution_matches_trackcheck_exactly(self, tmp_path):
        playlists_data = {
            "parity_playlist": [
                {"environment": "The Drawing Board", "track": "*Honk*", "mode": "Infinite Race"},
                "01 - Par For The Course",
                {"environment": "The Green", "track": "03 - Club House", "mode": "Classic Race"},
            ],
        }
        master_data = load_fixture("master_for_lint.json")

        fake_config_dir = tmp_path / "config"
        fake_config_dir.mkdir()
        fixture_playlists = fake_config_dir / "playlists.json"
        fixture_master = fake_config_dir / "master_tracks_list.json"
        fixture_playlists.write_text(json.dumps(playlists_data))
        fixture_master.write_text(json.dumps(master_data))

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        output_file = plugins_dir / "tracks_to_rotate.txt"

        control_playlists.resolve_and_write_playlist(
            "parity_playlist", shuffle_enabled=False, output_file=str(output_file),
            playlists_path=str(fixture_playlists), master_list_path=str(fixture_master),
        )

        written_lines = [
            line for line in output_file.read_text().splitlines() if line and not line.startswith("#")
        ]
        written_tuples = [tuple(line.split(",")) for line in written_lines]

        resolved, _ = resolve_playlist(playlists_data["parity_playlist"], master_data)

        assert written_tuples == resolved
        assert written_tuples  # sanity: the fixture actually resolved to something
