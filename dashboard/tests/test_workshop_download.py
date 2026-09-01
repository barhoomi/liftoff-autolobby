"""Tests for dashboard.control.workshop_download -- the in-game download watcher.

No Steam, no game: a fake "plugin" writes ``workshop_download_result.txt`` from inside
the fake ``sleep`` the watcher polls with, which is exactly the sequencing the real
plugin produces (request appears -> some time passes -> a result line lands) without any
of the machinery. That is what makes every branch -- each plugin failure reason, the
plugin-side timeout, the watcher's own bound, and the quarantine path -- testable in
milliseconds.

The property under test throughout: **an item only ever reaches the re-gather after
trackcheck passed it.** Everything else here is bookkeeping around that one rule.
"""

import os
import shutil

import pytest

from dashboard.control.protocol import ProtocolDir
from dashboard.control.workshop_download import (
    REASON_GATHER_FAILED,
    REASON_ITEM_DIR_MISSING,
    REASON_VALIDATION_FAILED,
    REASON_WATCHER_TIMEOUT,
    download_workshop_item,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "trackcheck", "tests", "fixtures")

WORKSHOP_ID = "3141592653"


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


class FakePlugin:
    """Stands in for plugin/WorkshopDownloader.cs: consumes the request file after
    ``delay_polls`` polls and writes the result line the real plugin would write."""

    def __init__(self, protocol, line, delay_polls=1):
        self.protocol = protocol
        self.line = line
        self.delay_polls = delay_polls
        self.polls = 0
        self.saw_request = False
        self.now = 0.0

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
            with open(self.protocol.path("workshop_download_result.txt"), "w") as fh:
                fh.write(self.line)


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
    outcome = download_workshop_item(
        WORKSHOP_ID, protocol,
        clock=plugin.clock, sleep=plugin.sleep, poll_interval=1.0, timeout=10.0,
        **kwargs)
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


class TestPluginReportedFailures:
    """Each of the plugin's four reason codes reaches the caller verbatim, and none of
    them lets the track database be touched."""

    @pytest.mark.parametrize("reason", [
        "bad_id",              # not a published-file id: no Steam call was even made
        "download_rejected",   # SteamUGC.DownloadItem returned false
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
        item = install(content_root, "no_gates_item")
        logger = RecordingLogger()
        plugin = FakePlugin(protocol, "{}|ok|\n".format(WORKSHOP_ID))
        outcome, calls = run(protocol, plugin, calls={}, logger=logger,
                             playlist_name="demo", tracks_file=str(tmp_path / "tracks.txt"))

        assert outcome.ok is False
        assert outcome.reason == REASON_VALIDATION_FAILED
        assert "GATE_DATA_MISSING" in outcome.validation_reasons
        assert calls == {}, "an unvalidated item must never reach gather/resolve"
        assert not os.path.exists(str(item)), "the rejected item was left in the content root"
        assert outcome.quarantine_path and os.path.isdir(outcome.quarantine_path)
        assert any(e == "quarantine" for e, _ in logger.events)

    def test_a_quarantine_failure_still_reports_the_rejection(self, protocol, content_root,
                                                             tmp_path):
        install(content_root, "no_gates_item")

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
