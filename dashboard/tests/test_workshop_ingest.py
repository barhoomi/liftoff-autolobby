"""dashboard.control.workshop_ingest.WorkshopIngest -- the non-blocking auto-ingest.

Driven exactly as ``test_bootstrap.py`` drives ``TrackBootstrap``: a fake clock and a
scripted ``tmp_path`` plugins directory, no game and no Steam.

The property this suite exists to enforce, and the reason the machine is a state machine
at all: **poll() never blocks.** It runs inside the orchestrator's 1-second monitor loop,
which is also the watchdog that relaunches a crashed game every 15 seconds. So the
``no_sleep`` fixture below replaces ``time.sleep`` with something that FAILS the test if
it is ever called, in every test in this file -- "never blocks" is checked by the suite,
not asserted in prose.

The second property: a ``/dl <track_id> <race_id>`` writes TWO result files, and
validating the first one alone would quarantine a perfectly good race item. The busy
marker is what tells the machine where the batch ends.
"""

import json
import os
import shutil
import time

import pytest

from dashboard.control.protocol import ProtocolDir
from dashboard.control.workshop_ingest import (
    BUSY_STALE_SECONDS,
    CLAIM_STALE_SECONDS,
    REASON_GAME_LISTING_MISSING,
    REASON_GATHER_FAILED,
    REASON_SWEEP_TIMEOUT,
    REASON_VALIDATION_FAILED,
    WorkshopIngest,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "trackcheck", "tests", "fixtures")

TRACK_ID = "3141592653"
RACE_ID = "2718281828"
SPLIT_TRACK = "Fixture Split Track"
ENVIRONMENT = "The Drawing Board"


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

    def decisions(self):
        return [f["detail"] for e, f in self.events if e == "decision"]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Any sleep anywhere under poll() fails the test. This is criterion 4j."""
    def forbidden(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError(
            "WorkshopIngest.poll() slept -- it runs inside the 1s monitor loop that is "
            "also the game watchdog, and must never block")

    monkeypatch.setattr(time, "sleep", forbidden)


@pytest.fixture
def protocol(tmp_path):
    proto = ProtocolDir(str(tmp_path / "plugins"))
    os.makedirs(proto.plugins_dir, exist_ok=True)
    return proto


@pytest.fixture
def content_root(tmp_path, monkeypatch):
    root = tmp_path / "workshop" / "content" / "410340"
    root.mkdir(parents=True)
    monkeypatch.setenv("FPV_WORKSHOP_CONTENT_DIR", str(root))
    return root


@pytest.fixture
def quarantine_dir(tmp_path, monkeypatch):
    path = tmp_path / "q"
    monkeypatch.setenv("FPV_QUARANTINE_DIR", str(path))
    return path


def install(content_root, fixture_name, workshop_id):
    dest = content_root / workshop_id
    shutil.copytree(os.path.join(FIXTURES, fixture_name), str(dest))
    return dest


def write_result(protocol, published_id, ok=True, reason=""):
    with open(protocol.path("workshop_download_result.txt"), "w") as fh:
        fh.write("{}|{}|{}\n".format(published_id, "ok" if ok else "fail", reason))


# Two dumps written microseconds apart can land on the SAME mtime (float seconds since
# the epoch have ~0.2us resolution at today's magnitude, and some filesystems are coarser
# still). A real sweep takes minutes, so this only ever bites tests -- stamp each write
# strictly forward so "a newer sweep landed" is expressible at test speed.
_DUMP_STAMP = [time.time()]


def write_dumps(protocol, tracks=(SPLIT_TRACK,), environment=ENVIRONMENT):
    with open(protocol.path("ui_tracks_dump.json"), "w") as fh:
        json.dump({environment: list(tracks)}, fh)
    with open(protocol.path("track_mode_availability.json"), "w") as fh:
        json.dump({environment: {"Classic Race": list(tracks)}}, fh)
    _DUMP_STAMP[0] += 1.0
    os.utime(protocol.path("track_mode_availability.json"),
             (_DUMP_STAMP[0], _DUMP_STAMP[0]))


def set_busy(protocol, count=1, age=0.0):
    path = protocol.path("workshop_download_busy.txt")
    with open(path, "w") as fh:
        fh.write("{}\n".format(count))
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))


class Harness:
    """A constructed machine plus the call counters its injected seams record."""

    def __init__(self, protocol, **kwargs):
        self.calls = {}
        self.clock = FakeClock()
        self.logger = RecordingLogger()
        kwargs.setdefault("logger", self.logger)
        kwargs.setdefault("gather", self._gather)
        kwargs.setdefault("resolve", self._resolve)
        self.ingest = WorkshopIngest(
            protocol, protocol.plugins_dir, clock=self.clock, poll_interval=0.0,
            sweep_timeout=100.0, **kwargs)

    def _gather(self):
        self.calls["gather"] = self.calls.get("gather", 0) + 1

    def _resolve(self, playlist_name, shuffle, tracks_file, logger=None):
        self.calls["resolve"] = self.calls.get("resolve", 0) + 1
        self.calls["playlist"] = playlist_name
        return ["a", "b"]

    def poll(self, times=1):
        for _ in range(times):
            self.clock.advance(1.0)
            self.ingest.poll()
        return self.ingest.state


@pytest.fixture
def harness(protocol, content_root, quarantine_dir):
    return Harness(protocol)


class TestIdle:
    def test_nothing_to_do_stays_idle_and_touches_nothing(self, harness):
        assert harness.poll(3) == WorkshopIngest.IDLE
        assert harness.calls == {}

    def test_a_half_written_result_is_left_alone(self, harness, protocol):
        with open(protocol.path("workshop_download_result.txt"), "w") as fh:
            fh.write("{}|o".format(TRACK_ID))
        assert harness.poll() == WorkshopIngest.IDLE
        assert protocol.exists("workshop_download_result.txt"), (
            "a torn line means 'not ready', never 'failed' -- deleting it would destroy "
            "the real result")


class TestBusyMarkerIsTheBatchBoundary:
    """Criterion 4i: a /dl <track> <race> writes two result files; validating the first
    alone would quarantine a perfectly good race item."""

    def _pair(self, content_root):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)

    def test_a_present_busy_marker_holds_the_batch_in_collecting(self, harness, protocol,
                                                                 content_root):
        self._pair(content_root)
        set_busy(protocol, count=2)
        write_result(protocol, TRACK_ID)
        assert harness.poll(3) == WorkshopIngest.COLLECTING
        assert "gather" not in harness.calls

    def test_the_pair_is_validated_as_a_set_once_the_marker_clears(self, harness, protocol,
                                                                   content_root, tmp_path):
        self._pair(content_root)
        set_busy(protocol, count=2)
        write_result(protocol, TRACK_ID)
        harness.poll(2)

        write_result(protocol, RACE_ID)
        harness.poll()                      # collects the second result
        os.remove(protocol.path("workshop_download_busy.txt"))

        assert harness.poll() == WorkshopIngest.WAITING_SWEEP
        assert harness.calls == {}, "gather must wait for a fresh availability sweep"

        write_dumps(protocol)
        assert harness.poll() == WorkshopIngest.FINALIZING
        assert harness.poll() == WorkshopIngest.IDLE
        assert harness.calls["gather"] == 1, harness.calls
        assert harness.ingest.last_outcome.ok is True
        assert sorted(harness.ingest.last_outcome.published_ids) == sorted([RACE_ID, TRACK_ID])

    def test_gather_runs_exactly_once_per_cycle(self, harness, protocol, content_root):
        self._pair(content_root)
        write_result(protocol, TRACK_ID)
        harness.poll()
        write_result(protocol, RACE_ID)
        write_dumps(protocol)
        harness.poll(6)
        assert harness.calls["gather"] == 1

    def test_a_stale_busy_marker_does_not_wedge_the_machine(self, harness, protocol,
                                                            content_root):
        """Criterion 4l: the plugin rewrites the marker every tick, so one whose mtime
        stopped advancing means the plugin died -- it must not disable auto-ingest."""
        self._pair(content_root)
        set_busy(protocol, count=2, age=BUSY_STALE_SECONDS + 60)
        write_result(protocol, TRACK_ID)
        harness.poll()                      # IDLE -> COLLECTING (baseline captured)
        write_dumps(protocol)               # the sweep the download armed
        harness.poll(4)
        assert harness.ingest.state == WorkshopIngest.IDLE
        assert harness.calls["gather"] == 1


class TestClaimArbitration:
    """Criterion 4k / §9.4: the CLI and this machine both poll one result file. Without
    arbitration the monitor eats the CLI's result and the CLI reports a false
    watcher_timeout while the ingest quietly succeeded."""

    def test_a_claimed_result_is_left_on_disk_and_not_consumed(self, harness, protocol,
                                                               content_root):
        install(content_root, "split_pair_track", TRACK_ID)
        protocol.claim_workshop_downloads([TRACK_ID])
        write_result(protocol, TRACK_ID)
        assert harness.poll(3) == WorkshopIngest.IDLE
        assert protocol.exists("workshop_download_result.txt")
        assert harness.calls == {}

    def test_a_stale_claim_is_ignored_and_deleted_and_the_result_is_ingested(
            self, harness, protocol, content_root):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        protocol.claim_workshop_downloads([TRACK_ID])
        stamp = time.time() - (CLAIM_STALE_SECONDS + 60)
        os.utime(protocol.path("workshop_download_claim.txt"), (stamp, stamp))

        write_result(protocol, TRACK_ID)
        write_dumps(protocol)
        harness.poll(4)
        assert not protocol.exists("workshop_download_claim.txt")
        assert harness.calls["gather"] == 1


class TestDeferredResults:
    def test_a_result_arriving_mid_cycle_starts_the_next_batch_not_this_one(
            self, harness, protocol, content_root):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        write_result(protocol, TRACK_ID)
        write_dumps(protocol)
        harness.poll(2)  # -> WAITING_SWEEP for the track alone

        write_result(protocol, RACE_ID)   # a second /dl lands mid-cycle
        harness.poll()
        assert not protocol.exists("workshop_download_result.txt"), (
            "the late result must be taken off disk -- the plugin writes ONE result "
            "file, so a third arrival would overwrite it")
        assert harness.calls.get("gather") is None, "it must not join the running batch"

        harness.poll(2)                   # first cycle finishes
        assert harness.calls["gather"] == 1
        harness.poll()                    # the held result starts its OWN cycle
        write_dumps(protocol)             # that download's own sweep lands
        harness.poll(3)

        assert harness.calls["gather"] == 2, (
            "the late result must start its own cycle, never be merged into a batch "
            "whose validation already ran")
        assert harness.ingest.state == WorkshopIngest.IDLE


class TestSweepTimeout:
    def test_a_dump_that_never_refreshes_ends_the_cycle_with_sweep_timeout(
            self, harness, protocol, content_root):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        write_result(protocol, TRACK_ID)
        harness.poll(2)
        assert harness.ingest.state == WorkshopIngest.WAITING_SWEEP

        harness.clock.advance(1000.0)
        harness.poll()
        assert harness.ingest.state == WorkshopIngest.IDLE
        assert harness.ingest.last_outcome.reason == REASON_SWEEP_TIMEOUT
        assert "rotation_paused=" in harness.ingest.last_outcome.detail
        assert harness.calls == {}

    def test_an_already_listed_track_does_not_wait_for_a_newer_dump(
            self, harness, protocol, content_root):
        """The disjunct in sweep_satisfied: a rotation's sweep can land between the plugin
        writing the result and this loop capturing its baseline. Without it, an ingest
        that already succeeded would sit and wait out its whole timeout."""
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        write_dumps(protocol)                      # sweep lands FIRST
        stamp = time.time() - 60
        os.utime(protocol.path("track_mode_availability.json"), (stamp, stamp))
        write_result(protocol, TRACK_ID)

        harness.poll(4)
        assert harness.calls["gather"] == 1
        assert harness.ingest.last_outcome.ok is True


class TestFailurePaths:
    def test_a_rejected_member_is_quarantined_unsubscribed_and_never_gathered(
            self, harness, protocol, content_root, quarantine_dir):
        item = install(content_root, "unsupported_env_item", TRACK_ID)
        write_result(protocol, TRACK_ID)
        write_dumps(protocol)
        harness.poll(3)
        assert harness.ingest.last_outcome.reason == REASON_VALIDATION_FAILED
        assert not os.path.exists(str(item))
        assert protocol.read_lines("workshop_unsubscribe_request.txt") == [TRACK_ID]
        assert harness.calls == {}

    def test_a_plugin_failure_is_reported_without_validating_anything(
            self, harness, protocol, content_root):
        write_result(protocol, TRACK_ID, ok=False, reason="k_EResultFileNotFound")
        harness.poll(2)
        assert harness.ingest.state == WorkshopIngest.IDLE
        assert harness.ingest.last_outcome.reason == "k_EResultFileNotFound"
        assert harness.calls == {}

    def test_a_track_the_dump_does_not_list_is_reported_not_quarantined(
            self, harness, protocol, content_root):
        item = install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        write_result(protocol, TRACK_ID)
        harness.poll()                                    # baseline captured
        write_dumps(protocol, tracks=("Some Other Track",))  # a sweep, but without us
        harness.poll(4)
        assert harness.ingest.last_outcome.reason == REASON_GAME_LISTING_MISSING
        assert os.path.isdir(str(item))

    def test_an_exception_in_gather_leaves_the_machine_back_in_idle(
            self, protocol, content_root, quarantine_dir):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)

        def exploding_gather():
            raise RuntimeError("master_tracks_list.json is unwritable")

        harness = Harness(protocol, gather=exploding_gather)
        write_result(protocol, TRACK_ID)
        write_dumps(protocol)
        harness.poll(4)
        assert harness.ingest.state == WorkshopIngest.IDLE
        assert harness.ingest.last_outcome.reason == REASON_GATHER_FAILED
        assert "unwritable" in harness.ingest.last_outcome.detail


class TestOutcomeReporting:
    def test_every_cycle_ends_with_exactly_one_decision_event(self, harness, protocol,
                                                              content_root):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        write_result(protocol, TRACK_ID)
        write_dumps(protocol)
        harness.poll(5)
        summaries = [d for d in harness.logger.decisions() if "installed and validated" in d]
        assert len(summaries) == 1, harness.logger.decisions()


class TestPlaylistIsReadLate:
    def test_the_playlist_is_read_at_finalize_time_not_at_construction(
            self, harness, protocol, content_root, tmp_path):
        """§9.3: a playlist switched between the /dl and the sweep is honoured for free,
        and the machine never holds a stale copy of loop state."""
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        harness.ingest.tracks_file = str(tmp_path / "tracks.txt")
        protocol.set_playlist_name("early")
        write_result(protocol, TRACK_ID)
        harness.poll(2)
        protocol.set_playlist_name("late")      # operator switches mid-cycle
        write_dumps(protocol)
        harness.poll(3)
        assert harness.calls["playlist"] == "late"

    def test_custom_means_refresh_the_database_but_rewrite_no_rotation(
            self, harness, protocol, content_root, tmp_path):
        install(content_root, "split_pair_track", TRACK_ID)
        install(content_root, "split_pair_race", RACE_ID)
        harness.ingest.tracks_file = str(tmp_path / "tracks.txt")
        protocol.set_playlist_name("custom")
        write_result(protocol, TRACK_ID)
        write_dumps(protocol)
        harness.poll(4)
        assert harness.calls["gather"] == 1
        assert "resolve" not in harness.calls


class TestUnsubscribeBatching:
    def test_two_rejected_members_produce_ONE_request_carrying_BOTH_ids(
            self, harness, protocol, content_root, quarantine_dir):
        """workshop_unsubscribe_request.txt is one-shot: the plugin reads and deletes it
        on its 1s tick. Writing it once per rejected member would mean the second write
        silently replaced the first, and the first item would stay subscribed -- free to
        be re-downloaded by Steam and listed by the next sweep."""
        install(content_root, "unsupported_env_item", TRACK_ID)
        install(content_root, "unsafe_name_item", RACE_ID)
        set_busy(protocol, count=2)
        write_result(protocol, TRACK_ID)
        harness.poll()
        write_result(protocol, RACE_ID)
        harness.poll()
        os.remove(protocol.path("workshop_download_busy.txt"))
        harness.poll(2)

        assert sorted(protocol.read_lines("workshop_unsubscribe_request.txt")) == \
            sorted([TRACK_ID, RACE_ID])
        assert harness.ingest.last_outcome.reason == REASON_VALIDATION_FAILED
