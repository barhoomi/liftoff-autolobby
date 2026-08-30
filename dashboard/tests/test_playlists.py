"""Tests for the playlist half of the control plane (``dashboard.control.playlists``).

These moved verbatim from ``orchestrator/tests/test_run_headless_lobby.py`` when
bot-dashboard.md's decision D5 moved the code under test out of the orchestrator; only
the imports changed. Keeping them as-is is the point -- they are the evidence that the
extraction was behaviour-preserving.
"""

import json
import os
import random

import pytest

from dashboard.control import playlists as rhl
from dashboard.control.playlists import (
    cross_validate_tracks,
    load_track_mode_availability,
    round_robin_shuffle_by_environment,
)

# Minimal env-normalization map in the shape cross_validate_tracks expects
# (lowercased variant -> canonical master-list key). The real one lives inside
# resolve_and_write_playlist(); these tests only need a representative slice.
ENV_NORM = {
    "bandocity": "Bando City",
    "bando city": "Bando City",
    "thedrawingboard": "The Drawing Board",
    "the drawing board": "The Drawing Board",
}


class TestCrossValidateTracks:
    def _tracks(self):
        return [("Track A", "Bando City", "Race")]

    def test_no_availability_data_fails_open(self):
        # None means the ground-truth dump was missing/unreadable -- every entry
        # must be kept untouched (fail open, never hang the rotation on missing data).
        tracks = [("A", "Bando City", "Race"), ("B", "The Green", "Freestyle")]
        kept, n_missing, n_mode = cross_validate_tracks(tracks, None, ENV_NORM)
        assert kept == tracks
        assert n_missing == 0
        assert n_mode == 0

    def test_environment_absent_from_dump_fails_open(self):
        availability = {"Some Other Env": {"Race": ["Track A"]}}
        kept, n_missing, n_mode = cross_validate_tracks(self._tracks(), availability, ENV_NORM)
        assert kept == self._tracks()
        assert (n_missing, n_mode) == (0, 0)

    def test_mode_absent_from_environment_fails_open(self):
        # The env is known but has no entry for this game mode -> no ground truth
        # for the (env, mode) pair -> keep.
        availability = {"Bando City": {}}
        kept, _, _ = cross_validate_tracks([("Track A", "Bando City", "Race")],
                                           availability, ENV_NORM)
        assert kept == [("Track A", "Bando City", "Race")]

    def test_track_offered_in_requested_mode_is_kept(self):
        availability = {"Bando City": {"Race": ["Track A", "Track B"]}}
        kept, n_missing, n_mode = cross_validate_tracks(self._tracks(), availability, ENV_NORM)
        assert kept == self._tracks()
        assert (n_missing, n_mode) == (0, 0)

    def test_track_match_is_case_and_whitespace_insensitive(self):
        availability = {"Bando City": {"Race": ["  TRACK a "]}}
        kept, _, _ = cross_validate_tracks([("track A", "Bando City", "Race")],
                                           availability, ENV_NORM)
        assert kept == [("track A", "Bando City", "Race")]

    def test_track_in_other_mode_only_is_dropped_as_mode_unsupported(self):
        availability = {"Bando City": {"Race": [], "Freestyle": ["Track A"]}}
        kept, n_missing, n_mode = cross_validate_tracks(self._tracks(), availability, ENV_NORM)
        assert kept == []
        assert n_missing == 0
        assert n_mode == 1

    def test_track_in_no_mode_is_dropped_as_not_installed(self):
        availability = {"Bando City": {"Race": ["Other Track"], "Freestyle": []}}
        kept, n_missing, n_mode = cross_validate_tracks(self._tracks(), availability, ENV_NORM)
        assert kept == []
        assert n_missing == 1
        assert n_mode == 0

    def test_env_key_is_normalized_before_dump_lookup(self):
        # The resolved track carries the variant spelling "BandoCity"; the dump is
        # keyed by the canonical "Bando City". Normalization must bridge the two.
        availability = {"Bando City": {"Race": ["Track A"]}}
        kept, _, _ = cross_validate_tracks([("Track A", "BandoCity", "Race")],
                                           availability, ENV_NORM)
        assert kept == [("Track A", "BandoCity", "Race")]

    def test_mixed_list_keeps_relative_order_of_survivors(self):
        tracks = [
            ("Keep 1", "Bando City", "Race"),
            ("Drop Missing", "Bando City", "Race"),
            ("Keep 2", "Bando City", "Race"),
            ("Drop Mode", "Bando City", "Race"),
        ]
        availability = {"Bando City": {
            "Race": ["Keep 1", "Keep 2"],
            "Freestyle": ["Drop Mode"],
        }}
        kept, n_missing, n_mode = cross_validate_tracks(tracks, availability, ENV_NORM)
        assert kept == [("Keep 1", "Bando City", "Race"), ("Keep 2", "Bando City", "Race")]
        assert n_missing == 1
        assert n_mode == 1

    def test_empty_input_list(self):
        kept, n_missing, n_mode = cross_validate_tracks([], {"Bando City": {"Race": []}}, ENV_NORM)
        assert kept == []
        assert (n_missing, n_mode) == (0, 0)


def _tracks_for(env, n):
    return [(f"{env} track {i}", env, "Race") for i in range(n)]


class TestRoundRobinShuffleByEnvironment:
    def test_result_is_a_permutation_of_the_input(self):
        random.seed(1)
        tracks = _tracks_for("EnvA", 3) + _tracks_for("EnvB", 2) + _tracks_for("EnvC", 4)
        result = round_robin_shuffle_by_environment(tracks)
        assert sorted(result) == sorted(tracks)

    def test_empty_input_yields_empty_output(self):
        assert round_robin_shuffle_by_environment([]) == []

    def test_single_environment_is_just_a_shuffle(self):
        random.seed(2)
        tracks = _tracks_for("OnlyEnv", 5)
        result = round_robin_shuffle_by_environment(tracks)
        assert sorted(result) == sorted(tracks)
        assert len(result) == 5

    def test_equal_groups_never_place_same_environment_back_to_back(self):
        # The whole point of round-robin over a flat shuffle: with equally sized
        # environment groups, consecutive entries always differ in environment.
        for seed in range(10):
            random.seed(seed)
            tracks = _tracks_for("EnvA", 4) + _tracks_for("EnvB", 4) + _tracks_for("EnvC", 4)
            result = round_robin_shuffle_by_environment(tracks)
            envs = [t[1] for t in result]
            for a, b in zip(envs, envs[1:]):
                assert a != b, f"seed {seed}: same env back-to-back in {envs}"

    def test_environments_cycle_with_fixed_period_while_all_have_tracks(self):
        # Each round appends exactly one track per environment in a fixed
        # (shuffled) env order, so envs repeat with period == number of envs.
        random.seed(3)
        tracks = _tracks_for("EnvA", 3) + _tracks_for("EnvB", 3)
        result = round_robin_shuffle_by_environment(tracks)
        envs = [t[1] for t in result]
        assert envs[0::2] == [envs[0]] * 3
        assert envs[1::2] == [envs[1]] * 3
        assert envs[0] != envs[1]

    def test_uneven_groups_still_emit_every_track(self):
        random.seed(4)
        tracks = _tracks_for("Big", 5) + _tracks_for("Small", 1)
        result = round_robin_shuffle_by_environment(tracks)
        assert sorted(result) == sorted(tracks)

    def test_deterministic_under_a_fixed_seed(self):
        tracks = _tracks_for("EnvA", 3) + _tracks_for("EnvB", 3)
        random.seed(42)
        first = round_robin_shuffle_by_environment(list(tracks))
        random.seed(42)
        second = round_robin_shuffle_by_environment(list(tracks))
        assert first == second


def _write_fixture_catalog(tmp_path, tracks_per_env=3):
    """A minimal, self-contained playlists.json + master_tracks_list.json pair, so
    resolve_and_write_playlist() can be exercised in full isolation without depending on
    the real repo's playlists.json/master_tracks_list.json (the latter is gitignored and
    only exists on a machine that has run gather_tracks.py against a live game install --
    see AGENTS.md -- so it is routinely ABSENT in a fresh checkout or CI, and
    resolve_and_write_playlist() raises MasterTracksMissingError outright when it's
    missing)."""
    playlists_path = tmp_path / "playlists.json"
    master_list_path = tmp_path / "master_tracks_list.json"
    playlists_path.write_text(json.dumps({
        "demo": [
            {"environment": "Bando City", "track": "*", "mode": "Race"},
            {"environment": "The Green", "track": "*", "mode": "Race"},
        ],
        "all_official_races": [{"environment": "*", "track": "*", "mode": "Race"}],
    }))
    master_list_path.write_text(json.dumps({
        "Bando City": {"official": [f"BC Track {i}" for i in range(tracks_per_env)]},
        "The Green": {"official": [f"Green Track {i}" for i in range(tracks_per_env)]},
    }))
    return str(playlists_path), str(master_list_path)


class TestResolveAndWritePlaylistDefinitionOrder:
    """bug-shuffle-toggle-and-tracks-incompatibility.md, Option 2: tracks_to_rotate.txt
    must always be written in playlist DEFINITION order now -- shuffling moved entirely
    into the plugin (Plugin.Rotation.cs). shuffle_enabled no longer reorders the written
    file; it only affects whether a stale plugin-owned shuffle deal gets invalidated."""

    def test_shuffle_enabled_does_not_reorder_the_written_file(self, tmp_path):
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path)
        out_unshuffled = tmp_path / "unshuffled" / "tracks_to_rotate.txt"
        out_unshuffled.parent.mkdir()
        out_shuffled = tmp_path / "shuffled" / "tracks_to_rotate.txt"
        out_shuffled.parent.mkdir()

        rhl.resolve_and_write_playlist("demo", False, str(out_unshuffled),
                                        playlists_path=playlists_path, master_list_path=master_list_path)
        rhl.resolve_and_write_playlist("demo", True, str(out_shuffled),
                                        playlists_path=playlists_path, master_list_path=master_list_path)

        assert out_unshuffled.read_text() == out_shuffled.read_text()
        # Sanity: the fixture actually resolved real tracks, not an empty/fallback file.
        assert "BC Track 0" in out_unshuffled.read_text()

    def test_clears_a_stale_plugin_owned_shuffle_order_file(self, tmp_path):
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path)
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        output_file = plugins_dir / "tracks_to_rotate.txt"
        shuffle_order_file = plugins_dir / "shuffle_order.txt"
        shuffle_order_file.write_text("# signature:deadbeef\n0\n1\n2\n")
        state_file = plugins_dir / "rotation_state.txt"
        state_file.write_text("5")

        rhl.resolve_and_write_playlist("demo", True, str(output_file),
                                        playlists_path=playlists_path, master_list_path=master_list_path)

        assert not shuffle_order_file.exists()
        assert state_file.read_text() == "0"

    def test_no_shuffle_order_file_present_is_a_silent_noop(self, tmp_path):
        # Must not error just because there was nothing to clear (e.g. the very first
        # launch, or shuffle has never been turned on for this install).
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path)
        output_file = tmp_path / "plugins" / "tracks_to_rotate.txt"
        output_file.parent.mkdir()

        rhl.resolve_and_write_playlist("demo", False, str(output_file),
                                        playlists_path=playlists_path, master_list_path=master_list_path)

        assert output_file.exists()
        assert not (output_file.parent / "shuffle_order.txt").exists()


class TestLoadTrackModeAvailability:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_track_mode_availability(str(tmp_path)) is None

    def test_malformed_json_returns_none(self, tmp_path):
        (tmp_path / "track_mode_availability.json").write_text("{not json", encoding="utf-8")
        assert load_track_mode_availability(str(tmp_path)) is None

    def test_valid_json_is_returned_parsed(self, tmp_path):
        data = {"Bando City": {"Race": ["Track A"]}}
        (tmp_path / "track_mode_availability.json").write_text(json.dumps(data), encoding="utf-8")
        assert load_track_mode_availability(str(tmp_path)) == data


class TestResolverErrorContract:
    """The two failure modes the D5 extraction re-shaped: they used to be a ValueError
    and a bare sys.exit(1) inside the library. Both are now exceptions the caller
    classifies -- the orchestrator still turns them into `ERROR: <msg>` + exit 1, while a
    web request handler can turn them into a 4xx instead of killing its own process."""

    def test_unknown_playlist_raises_playlist_not_found(self, tmp_path):
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path)
        with pytest.raises(rhl.PlaylistNotFoundError) as exc:
            rhl.resolve_and_write_playlist("nope", False, str(tmp_path / "tracks_to_rotate.txt"),
                                           playlists_path=playlists_path,
                                           master_list_path=master_list_path)
        assert "nope" in str(exc.value)

    def test_playlist_not_found_is_still_a_valueerror(self, tmp_path):
        # run_headless_lobby has always caught ValueError at this call site; keeping the
        # subclass relationship means the extraction did not silently widen that catch.
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path)
        with pytest.raises(ValueError):
            rhl.resolve_and_write_playlist("nope", False, str(tmp_path / "tracks_to_rotate.txt"),
                                           playlists_path=playlists_path,
                                           master_list_path=master_list_path)

    def test_missing_master_list_raises_instead_of_exiting(self, tmp_path):
        playlists_path, _ = _write_fixture_catalog(tmp_path)
        with pytest.raises(rhl.MasterTracksMissingError) as exc:
            rhl.resolve_and_write_playlist("demo", False, str(tmp_path / "tracks_to_rotate.txt"),
                                           playlists_path=playlists_path,
                                           master_list_path=str(tmp_path / "absent.json"))
        # Message shape is load-bearing: the orchestrator prints "ERROR: {e}", which must
        # stay byte-identical to what it printed before the move.
        assert str(exc.value).startswith("master_tracks_list.json not found at ")

    def test_missing_playlists_file_is_a_no_op(self, tmp_path, capsys):
        # Historical behaviour, deliberately preserved: warn and leave the existing
        # rotation file alone rather than blanking a working rotation.
        out = tmp_path / "tracks_to_rotate.txt"
        assert rhl.resolve_and_write_playlist("demo", False, str(out),
                                              playlists_path=str(tmp_path / "absent.json"),
                                              master_list_path=str(tmp_path / "absent2.json")) is None
        assert not out.exists()
        assert "Playlists file not found" in capsys.readouterr().out


class TestResolverWriteBehaviour:
    def test_returns_the_resolved_tuples_it_wrote(self, tmp_path):
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path, tracks_per_env=2)
        out = tmp_path / "plugins" / "tracks_to_rotate.txt"
        out.parent.mkdir()
        resolved = rhl.resolve_and_write_playlist("demo", False, str(out),
                                                  playlists_path=playlists_path,
                                                  master_list_path=master_list_path)
        assert len(resolved) == 4
        assert resolved[0] == ("BC Track 0", "Bando City", "Race")

    def test_falls_back_to_all_official_races_when_a_playlist_resolves_empty(self, tmp_path):
        playlists_path = tmp_path / "playlists.json"
        master_list_path = tmp_path / "master.json"
        playlists_path.write_text(json.dumps({
            "typo": [{"environment": "Bando City", "track": "no such track", "mode": "Race"}],
            "all_official_races": [{"environment": "*", "track": "*", "mode": "Race"}],
        }))
        master_list_path.write_text(json.dumps({"Bando City": {"official": ["BC Track 0"]}}))
        out = tmp_path / "plugins" / "tracks_to_rotate.txt"
        out.parent.mkdir()

        resolved = rhl.resolve_and_write_playlist("typo", False, str(out),
                                                  playlists_path=str(playlists_path),
                                                  master_list_path=str(master_list_path))
        assert resolved == [("BC Track 0", "Bando City", "Race")]
        assert "# Generated from playlist: all_official_races" in out.read_text()

    def test_write_goes_through_the_ownership_guarded_protocol_writer(self, tmp_path):
        # Regression guard for the rule the extraction exists to enforce: the resolver
        # resets rotation_state.txt via the sanctioned reset path, and never
        # content-writes it. A stray "5" here would mean someone reintroduced a direct
        # open() on plugin-owned state.
        playlists_path, master_list_path = _write_fixture_catalog(tmp_path)
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "rotation_state.txt").write_text("5")
        rhl.resolve_and_write_playlist("demo", False, str(plugins_dir / "tracks_to_rotate.txt"),
                                       playlists_path=playlists_path,
                                       master_list_path=master_list_path)
        assert (plugins_dir / "rotation_state.txt").read_text() == "0"
        assert not any(".tmp." in n for n in os.listdir(plugins_dir))
