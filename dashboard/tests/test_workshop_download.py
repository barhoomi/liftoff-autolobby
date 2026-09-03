"""Tests for dashboard.control.workshop_download -- the BLOCKING in-game download flow.

No Steam, no game: a fake "plugin" writes ``workshop_download_result.txt`` from inside
the fake ``sleep`` the watcher polls with, which is exactly the sequencing the real
plugin produces (request appears -> some time passes -> a result line lands) without any
of the machinery. That is what makes every branch -- each plugin failure reason, the
plugin-side timeout, the watcher's own bound, the quarantine path and now the sweep wait
-- testable in milliseconds.

The property under test throughout: **an item only ever reaches the re-gather after
trackcheck passed it AND the game's own availability sweep has re-run.** Everything else
here is bookkeeping around those two rules.

Since workshop-ingest-hardening.md the fake plugin also has to *write the dumps*: gather
runs only after a fresh availability sweep, because ``gather_tracks_and_races()`` rebuilds
the master list from ``ui_tracks_dump.json`` and gathering against a stale dump is the
exact failure (a downloaded track invisible for a month) this feature fixes.
"""

import json
import os
import shutil

import pytest

from dashboard.control.protocol import ProtocolDir
from dashboard.control.workshop_download import (
    REASON_GAME_LISTING_MISSING,
    REASON_GATHER_FAILED,
    REASON_ITEM_DIR_MISSING,
    REASON_SWEEP_TIMEOUT,
    REASON_VALIDATION_FAILED,
    REASON_WATCHER_TIMEOUT,
    download_workshop_item,
    download_workshop_items,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "trackcheck", "tests", "fixtures")

WORKSHOP_ID = "3141592653"
RACE_ID = "2718281828"

# What good_item / split_pair_track declare, so the fake dumps can list them.
GOOD_TRACK = "Fixture Good Track"
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


def write_dumps(protocol, tracks=(GOOD_TRACK,), environment=ENVIRONMENT):
    """Write the pair of dumps the plugin's Environment x GameMode sweep produces.

    Both, in that order, because ``track_dump_ready`` requires both to parse -- neither is
    written atomically by the plugin, so "the availability file has a new mtime" is not on
    its own evidence that a usable sweep landed.
    """
    os.makedirs(protocol.plugins_dir, exist_ok=True)
    with open(protocol.path("ui_tracks_dump.json"), "w") as fh:
        json.dump({environment: list(tracks)}, fh)
    with open(protocol.path("track_mode_availability.json"), "w") as fh:
        json.dump({environment: {"Classic Race": list(tracks)}}, fh)


class FakePlugin:
    """Stands in for plugin/WorkshopDownloader.cs: consumes each request file after
    ``delay_polls`` polls, writes the result line the real plugin would write, and (once
    every id has been answered) writes the dumps a re-sweep would produce."""

    def __init__(self, protocol, line, delay_polls=1, sweep_tracks=(GOOD_TRACK,)):
        self.protocol = protocol
        self.line = line
        self.delay_polls = delay_polls
        self.sweep_tracks = sweep_tracks
        self.polls = 0
        self.saw_request = False
        self.now = 0.0
        self.answered = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.polls += 1
        if self.protocol.exists("workshop_download_request.txt"):
            self.saw_request = True
        if self.line is not None and self.polls >= self.delay_polls:
            if self.protocol.exists("workshop_download_request.txt"):
                os.remove(self.protocol.path("workshop_download_request.txt"))
                self.answered += 1
            with open(self.protocol.path("workshop_download_result.txt"), "w") as fh:
                fh.write(self.line)
        if self.sweep_tracks is not None and not self.protocol.exists(
                "workshop_download_request.txt"):
            write_dumps(self.protocol, self.sweep_tracks)


class SequencePlugin(FakePlugin):
    """Answers a *sequence* of ids: ``lines[i]`` is the result for the i-th request."""

    def __init__(self, protocol, lines, sweep_tracks=(GOOD_TRACK, SPLIT_TRACK)):
        super().__init__(protocol, None, sweep_tracks=sweep_tracks)
        self.lines = list(lines)
        self.requests = []

    def sleep(self, seconds):
        self.now += seconds
        self.polls += 1
        if self.protocol.exists("workshop_download_request.txt"):
            self.saw_request = True
            requested = self.protocol.read_text("workshop_download_request.txt")
            self.requests.append(requested)
            os.remove(self.protocol.path("workshop_download_request.txt"))
            line = self.lines[len(self.requests) - 1]
            if line is not None:
                with open(self.protocol.path("workshop_download_result.txt"), "w") as fh:
                    fh.write(line)
            self.answered += 1
        elif self.sweep_tracks is not None:
            write_dumps(self.protocol, self.sweep_tracks)


@pytest.fixture
def protocol(tmp_path):
    return ProtocolDir(str(tmp_path / "plugins"))


@pytest.fixture
def content_root(tmp_path, monkeypatch):
    root = tmp_path / "workshop" / "content" / "410340"
    root.mkdir(parents=True)
    monkeypatch.setenv("FPV_WORKSHOP_CONTENT_DIR", str(root))
    return root


def install(content_root, fixture_name, workshop_id=WORKSHOP_ID):
    """Put a real trackcheck fixture where Steam would have put the downloaded item."""
    dest = content_root / workshop_id
    shutil.copytree(os.path.join(FIXTURES, fixture_name), str(dest))
    return dest


def run(protocol, plugin, **kwargs):
    calls = kwargs.setdefault("calls", {})
    kwargs.pop("calls")

    def gather():
        calls["gather"] = calls.get("gather", 0) + 1

    def resolve(playlist_name, shuffle, tracks_file, logger=None):
        calls["resolve"] = calls.get("resolve", 0) + 1
        return ["t1", "t2", "t3"]

    kwargs.setdefault("gather", gather)
    kwargs.setdefault("resolve", resolve)
    ids = kwargs.pop("ids", None)
    if ids is None:
        outcome = download_workshop_item(
            WORKSHOP_ID, protocol,
            clock=plugin.clock, sleep=plugin.sleep, poll_interval=1.0, timeout=10.0,
            sweep_timeout=30.0, **kwargs)
    else:
        outcome = download_workshop_items(
            ids, protocol,
            clock=plugin.clock, sleep=plugin.sleep, poll_interval=1.0, timeout=10.0,
            sweep_timeout=30.0, **kwargs)
    return outcome, calls


class TestSuccess:
    def test_valid_item_is_validated_then_gathered_and_resolved(self, protocol, content_root,
                                                                tmp_path):
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        calls = {}
        outcome, calls = run(protocol, plugin, calls=calls,
                             playlist_name="demo", tracks_file=str(tmp_path / "tracks.txt"))
        assert plugin.saw_request, "the watcher never wrote workshop_download_request.txt"
        assert outcome.ok is True
        assert outcome.reason == ""
        assert calls == {"gather": 1, "resolve": 1}
        assert outcome.resolved_count == 3

    def test_result_file_is_consumed_so_it_cannot_answer_the_next_request(
            self, protocol, content_root):
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        run(protocol, plugin, calls={})
        assert not protocol.exists("workshop_download_result.txt")

    def test_the_claim_file_is_always_released(self, protocol, content_root):
        """§9.4: an unreleased claim makes the orchestrator's auto-ingest ignore results
        until the staleness bound expires."""
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        run(protocol, plugin, calls={})
        assert not protocol.exists("workshop_download_claim.txt")

    def test_already_installed_is_a_success_carrying_its_reason(self, protocol, content_root):
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|already_installed\n".format(WORKSHOP_ID))
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is True
        assert outcome.reason == "already_installed"
        assert calls["gather"] == 1

    def test_without_a_playlist_the_database_is_refreshed_but_no_rotation_is_rewritten(
            self, protocol, content_root):
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, calls = run(protocol, plugin, calls={}, playlist_name=None)
        assert outcome.ok is True
        assert calls == {"gather": 1}
        assert outcome.resolved_count is None

    def test_a_gateless_track_now_ingests_with_a_warning(self, protocol, content_root):
        """§7: gateless/race-less tracks are ALLOWED. Which modes they are playable in is
        decided by the game and read back through the availability sweep -- trackcheck
        stops rejecting them outright."""
        install(content_root, "no_gates_item")
        logger = RecordingLogger()
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID),
                            sweep_tracks=("Fixture No Gates",))
        outcome, calls = run(protocol, plugin, calls={}, logger=logger,
                             ids=[WORKSHOP_ID])
        assert outcome.ok is True
        assert "GATE_DATA_MISSING" in outcome.warnings[WORKSHOP_ID]
        assert any(e == "decision" and "GATE_DATA_MISSING" in f["detail"]
                   for e, f in logger.events)


class TestPluginReportedFailures:
    """Each of the plugin's reason codes reaches the caller verbatim, and none of them
    lets the track database be touched."""

    @pytest.mark.parametrize("reason", [
        "bad_id",              # not a published-file id: no Steam call was even made
        "download_rejected",   # SteamUGC.DownloadItem returned false
        "queue_full",          # the /dl FIFO was full
        "k_EResultFileNotFound",  # DownloadItemResult_t carried a non-OK EResult
        "timeout",             # 120s with no DownloadItemResult_t at all
    ])
    def test_failure_reason_passes_through_untouched(self, protocol, content_root, reason):
        install(content_root, "good_item")  # even with files present: Steam said no
        plugin = FakePlugin(protocol, "{}|fail|{}\n".format(WORKSHOP_ID, reason))
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is False
        assert outcome.reason == reason
        assert calls == {}, "a failed download must never re-gather the track database"

    def test_failures_are_logged_as_errors(self, protocol, content_root):
        logger = RecordingLogger()
        plugin = FakePlugin(protocol, "{}|fail|bad_id\n".format(WORKSHOP_ID))
        run(protocol, plugin, calls={}, logger=logger)
        assert any(e == "error" and "bad_id" in f["message"] for e, f in logger.events)


class TestEmptyId:
    def test_an_empty_id_fails_fast_without_writing_a_request(self, protocol, content_root):
        plugin = FakePlugin(protocol, None)
        outcome = download_workshop_item("  ", protocol, clock=plugin.clock,
                                         sleep=plugin.sleep, timeout=10.0)
        assert outcome.ok is False
        assert outcome.reason == "bad_id"
        assert not protocol.exists("workshop_download_request.txt")
        assert not protocol.exists("workshop_download_claim.txt")
        assert plugin.polls == 0, "it should not have waited at all"


class TestWatcherTimeout:
    def test_no_result_at_all_times_out_with_its_own_reason(self, protocol, content_root):
        # `line=None` => the fake plugin never answers, i.e. the game isn't running.
        plugin = FakePlugin(protocol, None)
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is False
        assert outcome.reason == REASON_WATCHER_TIMEOUT
        assert calls == {}
        assert plugin.now >= 10.0

    def test_the_timeout_message_explains_the_dl_queue_interaction(self, protocol,
                                                                   content_root):
        """§4.3: a CLI request issued while an admin's /dl batch is downloading can
        out-wait its own bound. That is not a lost download, and the operator must not be
        left guessing."""
        plugin = FakePlugin(protocol, None)
        outcome, _ = run(protocol, plugin, calls={})
        assert "auto-ingest" in outcome.detail

    def test_a_half_written_result_line_is_not_mistaken_for_a_failure(self, protocol,
                                                                      content_root):
        """The plugin writes the result with File.WriteAllText, not an atomic replace, so
        a torn read is possible. It must read as 'not ready', never as 'failed' -- and it
        must not be deleted, or the real line would be lost."""
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|o".format(WORKSHOP_ID))
        outcome, _ = run(protocol, plugin, calls={})
        assert outcome.reason == REASON_WATCHER_TIMEOUT
        assert protocol.exists("workshop_download_result.txt")


class TestStaleAndForeignResults:
    def test_a_stale_result_from_a_previous_run_is_discarded_before_requesting(
            self, protocol, content_root):
        install(content_root, "good_item")
        protocol.write_text("lobby_name.txt", "x")  # creates the plugins dir
        with open(protocol.path("workshop_download_result.txt"), "w") as fh:
            fh.write("{}|fail|k_EResultTimeout\n".format(WORKSHOP_ID))
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, _ = run(protocol, plugin, calls={})
        assert outcome.ok is True, "the stale failure was mistaken for this request's answer"

    def test_a_result_for_another_id_does_not_end_the_wait(self, protocol, content_root):
        """An /dl command finishing in parallel writes a result for a different id. It is
        consumed (so it cannot linger and answer the next request) but never returned."""
        install(content_root, "good_item")

        class TwoStagePlugin(FakePlugin):
            def sleep(self, seconds):
                super().sleep(seconds)
                if self.polls == 3:
                    with open(self.protocol.path("workshop_download_result.txt"), "w") as fh:
                        fh.write("{}|ok|\n".format(WORKSHOP_ID))

        plugin = TwoStagePlugin(protocol, "999|ok|\n", delay_polls=1)
        logger = RecordingLogger()
        outcome, calls = run(protocol, plugin, calls={}, logger=logger)
        assert outcome.ok is True
        assert outcome.published_id == WORKSHOP_ID
        assert any(e == "decision" and "999" in f["detail"] for e, f in logger.events)


class TestValidationAndQuarantine:
    def test_an_invalid_track_is_quarantined_and_never_reaches_the_rotation(
            self, protocol, content_root, tmp_path, monkeypatch):
        monkeypatch.setenv("FPV_QUARANTINE_DIR", str(tmp_path / "q"))
        item = install(content_root, "unsupported_env_item")
        logger = RecordingLogger()
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, calls = run(protocol, plugin, calls={}, logger=logger,
                             playlist_name="demo", tracks_file=str(tmp_path / "tracks.txt"))

        assert outcome.ok is False
        assert outcome.reason == REASON_VALIDATION_FAILED
        assert "ENVIRONMENT_UNSUPPORTED" in outcome.validation_reasons
        assert calls == {}, "an unvalidated item must never reach gather/resolve"
        assert not os.path.exists(str(item)), "the rejected item was left in the content root"
        assert outcome.quarantine_path and os.path.isdir(outcome.quarantine_path)
        assert any(e == "quarantine" for e, _ in logger.events)

    def test_quarantining_also_asks_the_plugin_to_unsubscribe(self, protocol, content_root,
                                                              tmp_path, monkeypatch):
        """§1.4: without the unsubscribe, Steam re-downloads the rejected item and the
        next availability sweep lists it -- putting a track that FAILED validation back
        within the resolver's reach."""
        monkeypatch.setenv("FPV_QUARANTINE_DIR", str(tmp_path / "q"))
        install(content_root, "unsupported_env_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        run(protocol, plugin, calls={})
        assert protocol.read_lines("workshop_unsubscribe_request.txt") == [WORKSHOP_ID]

    def test_a_quarantine_failure_still_reports_the_rejection(self, protocol, content_root,
                                                              tmp_path):
        install(content_root, "unsupported_env_item")

        def exploding_quarantine(*args, **kwargs):
            raise OSError("read-only file system")

        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, calls = run(protocol, plugin, calls={}, quarantine=exploding_quarantine)
        assert outcome.ok is False
        assert outcome.reason == REASON_VALIDATION_FAILED
        assert outcome.quarantine_path is None
        assert calls == {}

    def test_steam_said_ok_but_no_files_landed(self, protocol, content_root):
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))  # nothing installed
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is False
        assert outcome.reason == REASON_ITEM_DIR_MISSING
        assert calls == {}


class TestGatherFailure:
    def test_a_failing_gather_is_reported_not_swallowed(self, protocol, content_root):
        install(content_root, "good_item")

        def exploding_gather():
            raise RuntimeError("master_tracks_list.json is unwritable")

        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, _ = run(protocol, plugin, calls={}, gather=exploding_gather)
        assert outcome.ok is False
        assert outcome.reason == REASON_GATHER_FAILED
        assert "unwritable" in outcome.detail


class TestSweepWait:
    """§2.4: gather must not run before a FRESH availability dump. Gathering against a
    stale one is the failure of evidence 4 -- ``gather_tracks_and_races()`` rebuilds the
    master list only from ``ui_tracks_dump.json``."""

    def test_a_dump_that_never_advances_times_out(self, protocol, content_root):
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID), sweep_tracks=None)
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is False
        assert outcome.reason == REASON_SWEEP_TIMEOUT
        assert calls == {}, "gather ran against a dump that never refreshed"

    def test_the_timeout_names_the_two_states_that_stop_a_sweep(self, protocol, content_root):
        """rotation_paused / rotation_engaged are operator state, not a bug: with either
        of them off the settings popup never re-opens and the sweep cannot happen."""
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID), sweep_tracks=None)
        outcome, _ = run(protocol, plugin, calls={})
        assert "rotation_paused=" in outcome.detail
        assert "rotation_engaged=" in outcome.detail

    def test_a_half_written_availability_dump_keeps_waiting(self, protocol, content_root):
        install(content_root, "good_item")

        class TornDumpPlugin(FakePlugin):
            def sleep(self, seconds):
                super().sleep(seconds)
                os.makedirs(self.protocol.plugins_dir, exist_ok=True)
                with open(self.protocol.path("track_mode_availability.json"), "w") as fh:
                    fh.write('{"The Drawing Board": {"Classic Ra')

        plugin = TornDumpPlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID), sweep_tracks=None)
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.reason == REASON_SWEEP_TIMEOUT
        assert calls == {}

    def test_a_fresh_and_readable_dump_lets_the_ingest_finish(self, protocol, content_root):
        install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is True
        assert calls == {"gather": 1}


class TestGameListingMissing:
    def test_a_track_the_fresh_dump_does_not_list_is_reported_not_quarantined(
            self, protocol, content_root, tmp_path, monkeypatch):
        """The files are fine; the game just did not list them (a subscription that did
        not take). Quarantining would destroy evidence for no reason."""
        monkeypatch.setenv("FPV_QUARANTINE_DIR", str(tmp_path / "q"))
        item = install(content_root, "good_item")
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID),
                            sweep_tracks=("Some Other Track",))
        outcome, calls = run(protocol, plugin, calls={})
        assert outcome.ok is False
        assert outcome.reason == REASON_GAME_LISTING_MISSING
        assert os.path.isdir(str(item)), "a listing miss must not quarantine the files"
        assert calls == {"gather": 1}, "gather runs first; only resolve is skipped"


class TestMultiIdBatch:
    """§4: a track and its race are separate workshop items, so the CLI takes both."""

    def test_a_split_pair_is_downloaded_in_order_and_validated_as_a_set(
            self, protocol, content_root, tmp_path):
        install(content_root, "split_pair_track", WORKSHOP_ID)
        install(content_root, "split_pair_race", RACE_ID)
        plugin = SequencePlugin(protocol, ["{}|ok|\n".format(WORKSHOP_ID),
                                           "{}|ok|\n".format(RACE_ID)],
                                sweep_tracks=(SPLIT_TRACK,))
        outcome, calls = run(protocol, plugin, calls={}, ids=[WORKSHOP_ID, RACE_ID],
                             playlist_name="demo", tracks_file=str(tmp_path / "tracks.txt"))
        assert plugin.requests == [WORKSHOP_ID, RACE_ID]
        assert outcome.ok is True, outcome.summary()
        assert sorted(outcome.item_dirs) == sorted([WORKSHOP_ID, RACE_ID])
        assert calls == {"gather": 1, "resolve": 1}

    def test_the_race_item_alone_is_rejected_and_quarantined(self, protocol, content_root,
                                                             tmp_path, monkeypatch):
        """The counterpart of the test above, and the reason set validation exists: the
        very same race item, ingested by itself, has no track to depend on."""
        monkeypatch.setenv("FPV_QUARANTINE_DIR", str(tmp_path / "q"))
        install(content_root, "split_pair_race", RACE_ID)
        plugin = SequencePlugin(protocol, ["{}|ok|\n".format(RACE_ID)], sweep_tracks=None)
        outcome, calls = run(protocol, plugin, calls={}, ids=[RACE_ID])
        assert outcome.ok is False
        assert outcome.reason == REASON_VALIDATION_FAILED
        assert "RACE_TRACK_DEP_MISSING" in outcome.validation_reasons[RACE_ID]
        assert calls == {}

    def test_the_batch_stops_at_the_first_plugin_failure(self, protocol, content_root,
                                                         tmp_path, monkeypatch):
        """§4.3 / criterion 4d: with the 2nd of 3 ids failing, exactly 2 requests are
        written, gather never runs, and NOTHING is quarantined -- a half-set's validation
        verdicts would be meaningless."""
        monkeypatch.setenv("FPV_QUARANTINE_DIR", str(tmp_path / "q"))
        install(content_root, "split_pair_track", WORKSHOP_ID)
        third = "1111111111"
        install(content_root, "split_pair_race", RACE_ID)
        quarantined = []
        plugin = SequencePlugin(protocol, ["{}|ok|\n".format(WORKSHOP_ID),
                                           "{}|fail|timeout\n".format(RACE_ID),
                                           "{}|ok|\n".format(third)])
        outcome, calls = run(protocol, plugin, calls={},
                             ids=[WORKSHOP_ID, RACE_ID, third],
                             quarantine=lambda *a, **k: quarantined.append(a))
        assert plugin.requests == [WORKSHOP_ID, RACE_ID]
        assert outcome.ok is False
        assert outcome.reason == "timeout"
        assert calls == {}
        assert quarantined == []

    def test_duplicate_ids_are_requested_once(self, protocol, content_root):
        install(content_root, "good_item")
        plugin = SequencePlugin(protocol, ["{}|ok|\n".format(WORKSHOP_ID)],
                                sweep_tracks=(GOOD_TRACK,))
        outcome, _ = run(protocol, plugin, calls={}, ids=[WORKSHOP_ID, WORKSHOP_ID])
        assert plugin.requests == [WORKSHOP_ID]
        assert outcome.ok is True
