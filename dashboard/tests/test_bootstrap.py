"""First-run track bootstrap (fresh-install-track-bootstrap-deadlock.md, option 3).

Everything here is pure decision logic exercised against tmp dirs and a fake clock —
no game, no Steam, no real master list. The three inputs the state machine reads from
the world are all files (``master_tracks_list.json``, ``ui_tracks_dump.json``,
``track_mode_availability.json``) and the two side effects it causes (gather, resolve)
are injected, so the full fresh-boot sequence is reproducible in milliseconds.
"""

import json

import pytest

from dashboard.control.bootstrap import (
    AVAILABILITY_DUMP_FILE,
    DECISION_KIND,
    DEFAULT_TIMEOUT_SECONDS,
    LEGACY_DUMP_FILE,
    TIMEOUT_ENV_VAR,
    TrackBootstrap,
    bootstrap_timeout,
    count_master_tracks,
    dump_environment_count,
    master_list_has_tracks,
    track_dump_ready,
)


# --- fakes -------------------------------------------------------------------


class FakeClock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class RecordingLogger:
    """Captures the structured events the bootstrap emits."""

    def __init__(self):
        self.decisions = []
        self.errors = []
        self.resolutions = []

    def decision(self, kind, detail):
        self.decisions.append({"kind": kind, "detail": detail})

    def playlist_resolved(self, playlist, track_count, **kwargs):
        # Emitted by the real resolver, which the bootstrap hands this logger to.
        self.resolutions.append({"playlist": playlist, "track_count": track_count})

    def error(self, message, context=None, playlist=None):
        self.errors.append({"message": message, "context": context, "playlist": playlist})


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def landed_dump(plugins_dir, tracks=("01 - Field Day",)):
    """Write the pair of files the plugin's sweep produces, both complete."""
    write_json(plugins_dir / LEGACY_DUMP_FILE, {"Straw Bale": list(tracks)})
    write_json(plugins_dir / AVAILABILITY_DUMP_FILE,
               {"Straw Bale": {"Infinite Race": list(tracks)}})


@pytest.fixture
def plugins_dir(tmp_path):
    d = tmp_path / "BepInEx" / "plugins"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def bootstrap_factory(plugins_dir, tmp_path):
    """Builds a TrackBootstrap wired to fakes; returns (bootstrap, clock, logger, calls)."""

    def _make(gather=None, resolve=None, timeout=100.0, poll_interval=5.0,
              playlist="all_official_races"):
        clock = FakeClock()
        logger = RecordingLogger()
        calls = {"gather": 0, "resolve": []}

        def _gather():
            calls["gather"] += 1

        def _resolve(name, shuffle, tracks_file, logger=None):
            calls["resolve"].append((name, shuffle, tracks_file))
            return [("01 - Field Day", "Straw Bale", "Infinite Race")]

        boot = TrackBootstrap(
            str(plugins_dir), playlist, str(tmp_path / "tracks_to_rotate.txt"),
            shuffle=True, logger=logger, timeout=timeout, poll_interval=poll_interval,
            clock=clock, gather=gather or _gather, resolve=resolve or _resolve)
        return boot, clock, logger, calls

    return _make


# --- master list emptiness ---------------------------------------------------


class TestMasterListDetection:
    def test_missing_file_is_not_populated(self, tmp_path):
        assert master_list_has_tracks(str(tmp_path / "nope.json")) is False

    def test_unparseable_file_is_not_populated(self, tmp_path):
        path = tmp_path / "master_tracks_list.json"
        path.write_text("{ this is not json")
        assert master_list_has_tracks(str(path)) is False

    def test_empty_object_is_not_populated(self, tmp_path):
        """Exactly what gather_tracks_and_races() writes when it finds nothing — the
        real fresh-install state, and the one the historical code sailed past."""
        path = tmp_path / "master_tracks_list.json"
        write_json(path, {})
        assert master_list_has_tracks(str(path)) is False

    def test_environments_with_no_tracks_are_not_populated(self, tmp_path):
        path = tmp_path / "master_tracks_list.json"
        write_json(path, {"Straw Bale": {"official": {}, "workshop": {}, "local": {}}})
        assert master_list_has_tracks(str(path)) is False

    def test_local_only_is_not_populated(self, tmp_path):
        """`local` tracks can never enter a rotation (they can't be shared), so a
        master list holding only those resolves to nothing just like an empty one."""
        path = tmp_path / "master_tracks_list.json"
        write_json(path, {"Straw Bale": {"official": {}, "local": {"Mine": []}}})
        assert master_list_has_tracks(str(path)) is False

    def test_populated_dict_shape(self, tmp_path):
        path = tmp_path / "master_tracks_list.json"
        write_json(path, {"Straw Bale": {"official": {"01 - Field Day": []}}})
        assert master_list_has_tracks(str(path)) is True

    def test_populated_list_shape(self, tmp_path):
        path = tmp_path / "master_tracks_list.json"
        write_json(path, {"Straw Bale": {"official": ["01 - Field Day"]}})
        assert master_list_has_tracks(str(path)) is True

    def test_count_across_environments_and_categories(self):
        assert count_master_tracks({
            "Straw Bale": {"official": {"a": [], "b": []}, "workshop": {"c": []}},
            "The Green": {"official": ["d"], "local": ["ignored"]},
        }) == 4

    def test_count_tolerates_junk(self):
        assert count_master_tracks(None) == 0
        assert count_master_tracks([]) == 0
        assert count_master_tracks({"Straw Bale": "not a dict"}) == 0


# --- dump readiness ----------------------------------------------------------


class TestTrackDumpReady:
    def test_no_files(self, plugins_dir):
        assert track_dump_ready(str(plugins_dir)) is False

    def test_legacy_dump_alone_is_not_enough(self, plugins_dir):
        """ui_tracks_dump.json is written first; on its own it may still be mid-sweep."""
        write_json(plugins_dir / LEGACY_DUMP_FILE, {"Straw Bale": ["01 - Field Day"]})
        assert track_dump_ready(str(plugins_dir)) is False

    def test_availability_alone_is_not_enough(self, plugins_dir):
        write_json(plugins_dir / AVAILABILITY_DUMP_FILE,
                   {"Straw Bale": {"Infinite Race": ["01 - Field Day"]}})
        assert track_dump_ready(str(plugins_dir)) is False

    def test_half_written_file_is_not_ready(self, plugins_dir):
        """The plugin writes these with File.WriteAllLines, not atomically — a torn read
        shows up as a JSON parse failure and must read as 'not yet', never as ready."""
        (plugins_dir / LEGACY_DUMP_FILE).write_text('{\n  "Straw Bale": ["01 - Fi')
        write_json(plugins_dir / AVAILABILITY_DUMP_FILE,
                   {"Straw Bale": {"Infinite Race": ["01 - Field Day"]}})
        assert track_dump_ready(str(plugins_dir)) is False

    def test_all_environments_empty_is_not_ready(self, plugins_dir):
        """A sweep that found no content anywhere would only regenerate another empty
        master list — keep waiting (and eventually time out) instead."""
        write_json(plugins_dir / LEGACY_DUMP_FILE, {"Straw Bale": [], "The Green": []})
        write_json(plugins_dir / AVAILABILITY_DUMP_FILE, {"Straw Bale": {"Infinite Race": []}})
        assert track_dump_ready(str(plugins_dir)) is False

    def test_complete_pair_is_ready(self, plugins_dir):
        landed_dump(plugins_dir)
        assert track_dump_ready(str(plugins_dir)) is True

    def test_environment_count(self, plugins_dir):
        assert dump_environment_count(str(plugins_dir)) == 0
        write_json(plugins_dir / LEGACY_DUMP_FILE, {"Straw Bale": ["a"], "The Green": ["b"]})
        assert dump_environment_count(str(plugins_dir)) == 2


# --- timeout configuration ---------------------------------------------------


class TestBootstrapTimeout:
    def test_default_when_unset(self):
        assert bootstrap_timeout({}) == DEFAULT_TIMEOUT_SECONDS

    def test_default_when_blank(self):
        assert bootstrap_timeout({TIMEOUT_ENV_VAR: "   "}) == DEFAULT_TIMEOUT_SECONDS

    def test_override(self):
        assert bootstrap_timeout({TIMEOUT_ENV_VAR: "42"}) == 42.0

    def test_zero_disables(self):
        assert bootstrap_timeout({TIMEOUT_ENV_VAR: "0"}) == 0.0

    def test_unparseable_falls_back_to_default(self):
        assert bootstrap_timeout({TIMEOUT_ENV_VAR: "soon"}) == DEFAULT_TIMEOUT_SECONDS


# --- the state machine -------------------------------------------------------


class TestTrackBootstrapStateMachine:
    def test_arms_idle_and_records_the_decision(self, bootstrap_factory):
        boot, _clock, logger, calls = bootstrap_factory()
        assert boot.state == TrackBootstrap.IDLE
        assert boot.active is True
        assert boot.deadline is None
        assert calls["gather"] == 0
        assert [d["kind"] for d in logger.decisions] == [DECISION_KIND]
        assert "first-run bootstrap armed" in logger.decisions[0]["detail"]

    def test_polling_before_the_game_starts_does_nothing(self, bootstrap_factory,
                                                         plugins_dir):
        boot, _clock, _logger, calls = bootstrap_factory()
        landed_dump(plugins_dir)  # even with the dump already there
        assert boot.poll() == TrackBootstrap.IDLE
        assert calls["gather"] == 0

    def test_game_start_arms_the_deadline(self, bootstrap_factory):
        boot, clock, _logger, _calls = bootstrap_factory(timeout=100.0)
        boot.note_game_started()
        assert boot.state == TrackBootstrap.WAITING
        assert boot.deadline == clock.now + 100.0

    def test_game_start_is_idempotent(self, bootstrap_factory):
        boot, clock, _logger, _calls = bootstrap_factory(timeout=100.0)
        boot.note_game_started()
        first_deadline = boot.deadline
        clock.advance(30)
        boot.note_game_started()
        assert boot.deadline == first_deadline

    def test_waits_then_completes_when_the_dump_lands(self, bootstrap_factory,
                                                      plugins_dir, tmp_path):
        boot, clock, logger, calls = bootstrap_factory(timeout=600.0, poll_interval=5.0)
        boot.note_game_started()

        # The plugin is still booting/sweeping.
        for _ in range(10):
            clock.advance(5)
            assert boot.poll() == TrackBootstrap.WAITING
        assert calls["gather"] == 0

        landed_dump(plugins_dir)
        clock.advance(5)
        assert boot.poll() == TrackBootstrap.COMPLETED

        assert calls["gather"] == 1
        assert calls["resolve"] == [("all_official_races", True,
                                     str(tmp_path / "tracks_to_rotate.txt"))]
        assert boot.resolved_count == 1
        assert boot.environment_count == 1
        assert boot.elapsed == pytest.approx(55.0)
        assert boot.active is False
        assert len(logger.decisions) == 2
        assert "track dump captured after 55s" in logger.decisions[1]["detail"]
        assert logger.errors == []

    def test_dump_ready_immediately_completes_on_first_poll(self, bootstrap_factory,
                                                            plugins_dir):
        """A restart after a failed first boot: the dump is already on disk from the
        previous run, so the bootstrap should finish on its first look."""
        boot, clock, _logger, calls = bootstrap_factory()
        landed_dump(plugins_dir)
        boot.note_game_started()
        clock.advance(1)
        assert boot.poll() == TrackBootstrap.COMPLETED
        assert calls["gather"] == 1

    def test_partial_dump_keeps_waiting_then_completes(self, bootstrap_factory,
                                                       plugins_dir):
        boot, clock, _logger, calls = bootstrap_factory(timeout=600.0)
        boot.note_game_started()

        write_json(plugins_dir / LEGACY_DUMP_FILE, {"Straw Bale": ["01 - Field Day"]})
        clock.advance(5)
        assert boot.poll() == TrackBootstrap.WAITING
        assert calls["gather"] == 0

        write_json(plugins_dir / AVAILABILITY_DUMP_FILE,
                   {"Straw Bale": {"Infinite Race": ["01 - Field Day"]}})
        clock.advance(5)
        assert boot.poll() == TrackBootstrap.COMPLETED

    def test_poll_is_rate_limited(self, bootstrap_factory, plugins_dir):
        boot, clock, _logger, calls = bootstrap_factory(poll_interval=5.0, timeout=600.0)
        boot.note_game_started()
        clock.advance(5)
        boot.poll()  # establishes the rate-limit window

        landed_dump(plugins_dir)
        clock.advance(1)
        assert boot.poll() == TrackBootstrap.WAITING  # too soon to look again
        assert calls["gather"] == 0

        assert boot.poll(force=True) == TrackBootstrap.COMPLETED

    def test_times_out_when_the_dump_never_lands(self, bootstrap_factory):
        boot, clock, logger, calls = bootstrap_factory(timeout=100.0, poll_interval=5.0)
        boot.note_game_started()

        clock.advance(99)
        assert boot.poll() == TrackBootstrap.WAITING

        clock.advance(5)
        assert boot.poll() == TrackBootstrap.TIMEOUT
        assert boot.active is False
        assert calls["gather"] == 0
        assert len(logger.errors) == 1
        assert logger.errors[0]["context"] == DECISION_KIND
        assert "timed out" in logger.errors[0]["message"]

    def test_dump_landing_wins_a_tie_with_the_deadline(self, bootstrap_factory,
                                                       plugins_dir):
        """Readiness is checked before the deadline: a dump that lands in the same poll
        the budget expires must still be used, not thrown away."""
        boot, clock, _logger, calls = bootstrap_factory(timeout=100.0)
        boot.note_game_started()
        landed_dump(plugins_dir)
        clock.advance(100)
        assert boot.poll() == TrackBootstrap.COMPLETED
        assert calls["gather"] == 1

    def test_retired_after_timeout(self, bootstrap_factory, plugins_dir):
        boot, clock, _logger, calls = bootstrap_factory(timeout=10.0)
        boot.note_game_started()
        clock.advance(11)
        assert boot.poll() == TrackBootstrap.TIMEOUT

        landed_dump(plugins_dir)
        clock.advance(60)
        assert boot.poll() == TrackBootstrap.TIMEOUT  # never revives itself
        assert calls["gather"] == 0

    def test_retired_after_completion(self, bootstrap_factory, plugins_dir):
        boot, clock, _logger, calls = bootstrap_factory()
        landed_dump(plugins_dir)
        boot.note_game_started()
        clock.advance(5)
        boot.poll()
        clock.advance(60)
        boot.poll()
        assert calls["gather"] == 1  # not re-run

    def test_gather_failure_is_contained(self, bootstrap_factory, plugins_dir):
        def _boom():
            raise RuntimeError("no write permission on config/")

        boot, clock, logger, _calls = bootstrap_factory(gather=_boom)
        landed_dump(plugins_dir)
        boot.note_game_started()
        clock.advance(5)
        assert boot.poll() == TrackBootstrap.FAILED
        assert boot.active is False
        assert len(logger.errors) == 1
        assert "no write permission" in logger.errors[0]["message"]
        assert logger.errors[0]["context"] == DECISION_KIND

    def test_resolve_failure_is_contained(self, bootstrap_factory, plugins_dir):
        def _boom(name, shuffle, tracks_file, logger=None):
            raise ValueError("playlist vanished")

        boot, clock, logger, calls = bootstrap_factory(resolve=_boom)
        landed_dump(plugins_dir)
        boot.note_game_started()
        clock.advance(5)
        assert boot.poll() == TrackBootstrap.FAILED
        assert calls["gather"] == 1  # gather still ran; the failure was downstream
        assert logger.errors[0]["playlist"] == "all_official_races"

    def test_resolving_to_zero_tracks_still_counts_as_completed(self, bootstrap_factory,
                                                                plugins_dir):
        """The bootstrap's job is to make discovery happen, not to guarantee a playlist
        matches. Zero resolved tracks is reported, not retried forever."""
        def _empty(name, shuffle, tracks_file, logger=None):
            return []

        boot, clock, logger, _calls = bootstrap_factory(resolve=_empty)
        landed_dump(plugins_dir)
        boot.note_game_started()
        clock.advance(5)
        assert boot.poll() == TrackBootstrap.COMPLETED
        assert boot.resolved_count == 0
        assert "to 0 tracks" in logger.decisions[1]["detail"]

    def test_works_without_a_logger(self, bootstrap_factory, plugins_dir, tmp_path):
        boot = TrackBootstrap(str(plugins_dir), "all_official_races",
                              str(tmp_path / "tracks.txt"), logger=None,
                              timeout=10.0, clock=FakeClock(),
                              gather=lambda: None,
                              resolve=lambda *a, **k: [])
        boot.note_game_started()
        assert boot.poll() in (TrackBootstrap.WAITING, TrackBootstrap.COMPLETED)


class TestBootstrapAgainstTheRealResolver:
    """The seam that matters: once the dump is reconciled into a master list, the SAME
    resolver the normal startup path uses must turn it into a real rotation file. Only
    the gather step is faked (it needs a game install); resolution is the real function,
    including its cross-validation against the plugin's availability dump."""

    def test_dump_to_working_rotation(self, plugins_dir, tmp_path):
        from dashboard.control.playlists import resolve_and_write_playlist

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        playlists_path = config_dir / "playlists.json"
        write_json(playlists_path, {"all_official_races": [
            {"environment": "Straw Bale", "track": "01 - Field Day", "mode": "Infinite Race"},
        ]})
        master_path = config_dir / "master_tracks_list.json"

        # Fresh install: the master list is the empty object gather writes when it finds
        # nothing, so the bootstrap is exactly what should be armed here.
        write_json(master_path, {})
        assert master_list_has_tracks(str(master_path)) is False

        def fake_gather():
            """Stands in for gather_tracks_and_races()'s ui_tracks_dump.json branch."""
            dump = json.loads((plugins_dir / LEGACY_DUMP_FILE).read_text())
            write_json(master_path, {env: {"official": {name: [] for name in names},
                                           "workshop": {}, "local": {}}
                                     for env, names in dump.items()})

        def real_resolve(name, shuffle, tracks_file, logger=None):
            return resolve_and_write_playlist(name, shuffle, tracks_file, logger=logger,
                                              playlists_path=str(playlists_path),
                                              master_list_path=str(master_path))

        clock = FakeClock()
        logger = RecordingLogger()
        boot = TrackBootstrap(str(plugins_dir), "all_official_races",
                              str(plugins_dir / "tracks_to_rotate.txt"),
                              logger=logger, timeout=600.0, clock=clock,
                              gather=fake_gather, resolve=real_resolve)
        boot.note_game_started()

        clock.advance(10)
        assert boot.poll() == TrackBootstrap.WAITING
        assert not (plugins_dir / "tracks_to_rotate.txt").exists()

        landed_dump(plugins_dir)
        clock.advance(10)
        assert boot.poll() == TrackBootstrap.COMPLETED

        assert master_list_has_tracks(str(master_path)) is True
        assert boot.resolved_count == 1
        rotation = (plugins_dir / "tracks_to_rotate.txt").read_text().splitlines()
        assert rotation[0].startswith("#")
        assert rotation[1] == "01 - Field Day,Straw Bale,Infinite Race"
        # The resolver's own post-write housekeeping ran too (fresh cursor for the
        # rotation that just appeared out of nowhere), and it emitted the usual
        # playlist_resolved event — the bootstrap adds a `decision` around the normal
        # flow rather than replacing any of it.
        assert (plugins_dir / "rotation_state.txt").read_text().strip() == "0"
        assert logger.resolutions == [{"playlist": "all_official_races", "track_count": 1}]
        assert [d["kind"] for d in logger.decisions] == [DECISION_KIND, DECISION_KIND]
        assert logger.errors == []


class TestEventLoggerDecision:
    """The `decision` event the bootstrap emits must match the shared JSONL schema."""

    def test_decision_event_shape(self, tmp_path):
        from event_log import EventLogger

        logger = EventLogger(str(tmp_path))
        record = logger.decision(DECISION_KIND, "armed")
        assert list(record)[:3] == ["ts", "source", "event"]
        assert record["source"] == "orchestrator"
        assert record["event"] == "decision"
        assert record["kind"] == DECISION_KIND
        assert record["detail"] == "armed"

        written = json.loads((tmp_path / logger.current_path().split("/")[-1]).read_text())
        assert written == record
