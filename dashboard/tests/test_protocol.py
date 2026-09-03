"""Tests for dashboard.control.protocol — the single writer of the plugin's plain-text
protocol files.

The three properties worth guarding are the three reasons the module exists: ownership
enforcement (plugin-owned state can only be reset), atomic replacement (the plugin polls
these files every second and must never see a torn one) and the typed setters producing
byte-exactly what the plugin's parsers expect.
"""

import os
import re
import threading

import pytest

from dashboard.control.protocol import (
    MAX_SAY_MESSAGE_LENGTH,
    READ_ONLY,
    RESET_ONLY,
    WRITABLE,
    ProtocolDir,
    ProtocolOwnershipError,
    parse_track_line,
    parse_workshop_download_result,
)


@pytest.fixture
def proto(tmp_path):
    return ProtocolDir(str(tmp_path / "plugins"))


class TestOwnership:
    def test_plugin_owned_files_cannot_be_content_written(self, proto):
        for name in RESET_ONLY:
            with pytest.raises(ProtocolOwnershipError):
                proto.write_text(name, "anything")

    def test_plugin_owned_files_cannot_be_deleted_directly(self, proto):
        with pytest.raises(ProtocolOwnershipError):
            proto.delete("shuffle_order.txt")

    def test_plugin_produced_data_is_read_only(self, proto):
        for name in READ_ONLY:
            with pytest.raises(ProtocolOwnershipError):
                proto.write_text(name, "{}")

    def test_rotation_state_reset_is_the_sanctioned_write(self, proto):
        proto.reset_rotation_state()
        assert proto.read_text("rotation_state.txt") == "0"

    def test_clear_shuffle_order_removes_the_file_and_reports_whether_it_existed(self, proto):
        proto.write_flag("auto_start.txt", True)  # creates the directory
        target = proto.path("shuffle_order.txt")
        with open(target, "w") as f:
            f.write("# signature:deadbeef\n0\n1\n")
        assert proto.clear_shuffle_order() is True
        assert not os.path.exists(target)
        assert proto.clear_shuffle_order() is False  # already gone: silent no-op

    def test_ownership_tables_do_not_overlap(self):
        assert not (set(WRITABLE) & set(RESET_ONLY))
        assert not (set(WRITABLE) & set(READ_ONLY))
        assert not (set(RESET_ONLY) & set(READ_ONLY))


class TestWrites:
    def test_write_creates_the_directory(self, tmp_path):
        proto = ProtocolDir(str(tmp_path / "missing" / "plugins"))
        proto.set_lobby_name("Procedural Loop Room")
        assert proto.read_text("lobby_name.txt") == "Procedural Loop Room"

    def test_write_leaves_no_temp_files_behind(self, proto):
        proto.set_lobby_name("x")
        proto.set_rotation_interval(90)
        leftovers = [n for n in os.listdir(proto.plugins_dir) if ".tmp." in n]
        assert leftovers == []

    def test_write_replaces_rather_than_truncates(self, proto):
        """os.replace swaps a fully written file in. A reader holding the old path sees
        the whole old content, never a zero-length window -- which truncate-then-write
        (the pre-D5 `open(..., "w")`) could expose to the plugin's 1s poll."""
        proto.set_lobby_name("old name")
        old_inode = os.stat(proto.path("lobby_name.txt")).st_ino
        proto.set_lobby_name("new name")
        assert proto.read_text("lobby_name.txt") == "new name"
        assert os.stat(proto.path("lobby_name.txt")).st_ino != old_inode

    def test_lock_is_reentrant_across_sequential_writes(self, proto):
        for i in range(5):
            proto.set_rotation_interval(i)
        assert proto.read_int("rotation_interval.txt") == 4

    def test_unknown_file_names_are_allowed(self, proto):
        # The table gates plugin-owned files; it is not an allowlist, so a protocol file
        # added by a future plugin feature does not need this module edited first.
        proto.write_text("some_future_flag.txt", "true")
        assert proto.read_flag("some_future_flag.txt") is True


class TestTypedSetters:
    def test_flags_use_the_plugins_true_false_wording(self, proto):
        proto.set_auto_start(True)
        proto.set_room_private(False)
        assert proto.read_text("auto_start.txt") == "true"
        assert proto.read_text("room_private.txt") == "false"

    def test_interval_is_written_as_a_bare_integer(self, proto):
        proto.set_rotation_interval(90.7)
        assert proto.read_text("rotation_interval.txt") == "90"

    def test_available_playlists_is_newline_terminated_per_entry(self, proto):
        proto.set_available_playlists(["a", "b"])
        assert proto.read_text("available_playlists.txt") == "a\nb"
        with open(proto.path("available_playlists.txt")) as f:
            assert f.read() == "a\nb\n"

    def test_maintenance_set_creates_and_cancel_deletes(self, proto):
        # The plugin's external-maintenance check is presence-based, and it cancels when
        # the file disappears -- writing "false" would schedule a shutdown, not cancel one.
        assert proto.set_maintenance(True) is True
        assert proto.exists("maintenance_active.txt")
        assert proto.set_maintenance(False) is True
        assert not proto.exists("maintenance_active.txt")
        assert proto.set_maintenance(False) is False

    def test_trigger_skip_now_writes_the_one_shot_file(self, proto):
        # No "cancel" side by design (unlike set_maintenance): the plugin deletes the
        # file itself once HandleGameRoom consumes it, so the control plane's only
        # sanctioned action is to create it.
        proto.trigger_skip_now()
        assert proto.exists("skip_now.txt")
        assert proto.read_text("skip_now.txt") == "true"

    def test_request_workshop_download_writes_a_bare_id(self, proto):
        # One-shot like skip_now.txt: the plugin deletes it the instant it starts
        # processing, so there is no cancel side and no second "is a download pending".
        proto.request_workshop_download(" 1234567890 ")
        assert proto.read_text("workshop_download_request.txt") == "1234567890"

    def test_request_workshop_download_refuses_an_empty_id(self, proto):
        # An empty request file is unanswerable: the plugin has no id to echo, so its
        # result line would be unparseable and the requester would wait out its timeout.
        with pytest.raises(ValueError):
            proto.request_workshop_download("   ")

    def test_override_game_mode_clears_on_falsy(self, proto):
        proto.set_override_game_mode("Classic Race")
        assert proto.read_text("override_game_mode.txt") == "Classic Race"
        proto.set_override_game_mode(None)
        assert not proto.exists("override_game_mode.txt")


class TestReads:
    def test_missing_file_returns_the_default(self, proto):
        assert proto.read_text("nope.txt") is None
        assert proto.read_text("nope.txt", "fallback") == "fallback"
        assert proto.read_flag("nope.txt") is False
        assert proto.read_flag("nope.txt", True) is True
        assert proto.read_int("nope.txt", 600) == 600

    def test_read_flag_matches_the_plugins_FileFlag_semantics(self, proto):
        # plugin: File.Exists(p) && content.Trim() == "true" (case-insensitive)
        proto.write_text("democracy_mode.txt", "TRUE\n")
        assert proto.read_flag("democracy_mode.txt") is True
        proto.write_text("democracy_mode.txt", "1")
        assert proto.read_flag("democracy_mode.txt") is False

    def test_read_int_tolerates_the_plugins_float_formatting(self, proto):
        # /interval writes newInterval.ToString("F0"), but a hand-edited file may hold
        # "90.0" -- both must read back as 90 rather than raising.
        proto.write_text("rotation_interval.txt", "90.0")
        assert proto.read_int("rotation_interval.txt") == 90
        proto.write_text("rotation_interval.txt", "banana")
        assert proto.read_int("rotation_interval.txt", 600) == 600

    def test_rotation_tracks_are_parsed_skipping_the_header_comment(self, proto):
        proto.write_text(
            "tracks_to_rotate.txt",
            "# Generated from playlist: demo\n"
            "BC Track 0,Bando City,Race\n"
            "Green Track 1,The Green,Infinite Race\n",
        )
        assert proto.read_rotation_tracks() == [
            {"track": "BC Track 0", "environment": "Bando City", "mode": "Race"},
            {"track": "Green Track 1", "environment": "The Green", "mode": "Infinite Race"},
        ]

    def test_rotation_tracks_of_a_missing_file_is_empty(self, proto):
        assert proto.read_rotation_tracks() == []

    def test_rotation_tracks_preserves_a_comma_in_the_track_name(self, proto):
        """bug-comma-in-track-name.md: the operator's live report -- a real Liftoff
        track literally named "Iceberg, Right ahead!" (comma AND exclamation point)."""
        proto.write_text(
            "tracks_to_rotate.txt",
            "# Generated from playlist: demo\n"
            "Iceberg, Right ahead!,Bando City,Race\n",
        )
        assert proto.read_rotation_tracks() == [
            {"track": "Iceberg, Right ahead!", "environment": "Bando City", "mode": "Race"},
        ]

    def test_read_static_track_lines_skips_headers_and_blanks_but_keeps_raw_lines(self, proto):
        proto.write_text(
            "tracks_to_rotate.txt",
            "# Generated from playlist: demo\n"
            "\n"
            "BC Track 0, Bando City, Race\n"
            "Green Track 1,The Green,Infinite Race\n",
        )
        # Note the exact whitespace difference between the two lines is preserved --
        # shuffle_order.py's signature check needs the plugin's own byte-identical
        # trimmed lines, not a reconstruction from parsed fields.
        assert proto.read_static_track_lines() == [
            "BC Track 0, Bando City, Race",
            "Green Track 1,The Green,Infinite Race",
        ]

    def test_read_static_track_lines_of_a_missing_file_is_empty(self, proto):
        assert proto.read_static_track_lines() == []


class TestWorkshopDownloadResult:
    """workshop_download_result.txt is plugin-produced (READ_ONLY) but the control plane
    owns exactly one mutation of it: consuming it. Same shape as rotation_state.txt's
    reset."""

    def test_the_result_file_cannot_be_written_by_the_control_plane(self, proto):
        with pytest.raises(ProtocolOwnershipError):
            proto.write_text("workshop_download_result.txt", "1|ok|")

    def test_consume_reads_then_deletes(self, proto):
        proto.write_text("lobby_name.txt", "x")  # creates the directory
        with open(proto.path("workshop_download_result.txt"), "w") as f:
            f.write("1234567890|ok|\n")
        assert proto.consume_workshop_download_result() == {
            "published_id": "1234567890", "ok": True, "reason": "",
        }
        assert not proto.exists("workshop_download_result.txt")
        assert proto.consume_workshop_download_result() is None

    def test_a_half_written_line_is_left_alone(self, proto):
        # The plugin writes with File.WriteAllText (not an atomic replace), so a torn read
        # is real. Deleting it would destroy the answer that is still being written.
        proto.write_text("lobby_name.txt", "x")
        with open(proto.path("workshop_download_result.txt"), "w") as f:
            f.write("1234567890|o")
        assert proto.consume_workshop_download_result() is None
        assert proto.exists("workshop_download_result.txt")

    def test_parse_accepts_only_complete_records(self):
        assert parse_workshop_download_result("42|fail|timeout") == {
            "published_id": "42", "ok": False, "reason": "timeout",
        }
        # a reason may itself be empty (plain success) but the field must be there
        assert parse_workshop_download_result("42|ok|")["reason"] == ""
        for bad in (None, "", "42", "42|ok", "|ok|", "42|maybe|x"):
            assert parse_workshop_download_result(bad) is None

    def test_parse_keeps_a_pipe_inside_the_reason(self):
        # split("|", 2): only the first two pipes are field separators, so an EResult name
        # or message containing one survives instead of silently truncating.
        assert parse_workshop_download_result("42|fail|a|b")["reason"] == "a|b"


def test_plugins_dir_is_required():
    with pytest.raises(ValueError):
        ProtocolDir("")


class TestParseTrackLine:
    """bug-comma-in-track-name.md: rightmost-split into exactly 3 fields, mirroring the
    plugin's ParseTrackLine (plugin/Plugin.Rotation.cs)."""

    def test_plain_no_comma_name(self):
        assert parse_track_line("BC Track 0,Bando City,Race") == (
            "BC Track 0", "Bando City", "Race",
        )

    def test_operator_reported_track_name_with_a_comma(self):
        assert parse_track_line("Iceberg, Right ahead!,Bando City,Race") == (
            "Iceberg, Right ahead!", "Bando City", "Race",
        )

    def test_track_name_with_a_comma_and_a_space_after_it(self):
        # The writer's own join style ("TrackName, Environment, GameMode") -- the space
        # after the internal comma is part of the track name and must survive verbatim.
        assert parse_track_line("Iceberg, Right ahead!, Bando City, Race") == (
            "Iceberg, Right ahead!", "Bando City", "Race",
        )

    def test_hypothetical_two_comma_track_name(self):
        assert parse_track_line("A, B, C,Bando City,Race") == (
            "A, B, C", "Bando City", "Race",
        )

    def test_missing_trailing_fields_default_to_empty(self):
        assert parse_track_line("SoloName") == ("SoloName", "", "")
        assert parse_track_line("Name,Env") == ("Name", "Env", "")

    def test_empty_line(self):
        assert parse_track_line("") == ("", "", "")


class TestEnqueueSay:
    """dashboard-chat-send.md: sequenced single-shot say_<seq>.txt files. The three
    properties that matter are the ones the file convention was picked for (see the
    feature doc's Decision log): sequence assignment can't collide, invalid messages
    never reach a file, and a consumed (deleted) file can't be "replayed"."""

    def test_first_message_gets_sequence_1(self, proto):
        seq = proto.enqueue_say("gg well flown")
        assert seq == 1
        assert proto.read_text("say_1.txt") == "gg well flown"

    def test_sequence_increments_across_calls(self, proto):
        assert proto.enqueue_say("one") == 1
        assert proto.enqueue_say("two") == 2
        assert proto.enqueue_say("three") == 3
        assert proto.read_text("say_1.txt") == "one"
        assert proto.read_text("say_2.txt") == "two"
        assert proto.read_text("say_3.txt") == "three"

    def test_a_consumed_sequence_number_may_be_reused_safely(self, proto):
        # The plugin deletes say_1.txt after sending it (consumed-exactly-once). The
        # next number is derived from what's ON DISK right now, not a persisted
        # counter (no second piece of state to keep in sync, AGENTS.md rule 4) -- so
        # once say_1.txt is gone, a fresh enqueue_say is free to reuse "1" for
        # genuinely new content. This is not a "replay": the old message was already
        # sent and its file no longer exists: nothing is duplicated in chat, and the
        # plugin has no way to observe that the number was used before.
        proto.enqueue_say("first")
        os.remove(proto.path("say_1.txt"))
        seq = proto.enqueue_say("second")
        assert seq == 1
        assert proto.read_text("say_1.txt") == "second"

    def test_sequence_continues_from_the_highest_existing_file_even_with_gaps(self, proto):
        proto.enqueue_say("a")  # say_1.txt
        proto.enqueue_say("b")  # say_2.txt
        os.remove(proto.path("say_1.txt"))  # consumed out of order somehow; say_2.txt remains
        seq = proto.enqueue_say("c")
        assert seq == 3

    def test_message_is_written_verbatim_no_trailing_newline_added(self, proto):
        proto.enqueue_say("  hello there  ")
        # Leading/trailing whitespace around the whole message is trimmed (it is not
        # meaningful chat content), but the write itself has no added terminator.
        with open(proto.path("say_1.txt"), encoding="utf-8") as f:
            assert f.read() == "hello there"

    def test_write_is_atomic_temp_file_replace(self, proto):
        proto.enqueue_say("hello")
        leftovers = [n for n in os.listdir(proto.plugins_dir) if ".tmp." in n]
        assert leftovers == []

    def test_creates_the_directory(self, tmp_path):
        proto = ProtocolDir(str(tmp_path / "missing" / "plugins"))
        seq = proto.enqueue_say("hi")
        assert seq == 1
        assert proto.read_text("say_1.txt") == "hi"

    def test_empty_message_is_rejected(self, proto):
        with pytest.raises(ValueError):
            proto.enqueue_say("")

    def test_whitespace_only_message_is_rejected(self, proto):
        with pytest.raises(ValueError):
            proto.enqueue_say("   \n\t  ")

    def test_none_message_is_rejected(self, proto):
        with pytest.raises(ValueError):
            proto.enqueue_say(None)

    def test_rejected_message_does_not_consume_a_sequence_number_or_write_a_file(self, proto):
        with pytest.raises(ValueError):
            proto.enqueue_say("")
        # Validation happens before the directory is ever created.
        assert not os.path.exists(proto.plugins_dir)
        seq = proto.enqueue_say("first real message")
        assert seq == 1

    def test_message_at_the_length_limit_is_accepted(self, proto):
        message = "x" * MAX_SAY_MESSAGE_LENGTH
        seq = proto.enqueue_say(message)
        assert proto.read_text("say_{}.txt".format(seq)) == message

    def test_message_over_the_length_limit_is_rejected(self, proto):
        with pytest.raises(ValueError):
            proto.enqueue_say("x" * (MAX_SAY_MESSAGE_LENGTH + 1))

    def test_slash_prefixed_message_is_accepted_verbatim(self, proto):
        # dashboard-chat-send.md decision log: no command execution in v1 -- a
        # "/"-prefixed message is just another chat message, not special-cased here.
        seq = proto.enqueue_say("/kick somebody")
        assert proto.read_text("say_{}.txt".format(seq)) == "/kick somebody"

    def test_two_writers_racing_for_the_lock_still_get_distinct_sequence_numbers(self, proto):
        # Simulates the orchestrator and the dashboard calling enqueue_say concurrently:
        # both go through the same flock, so the scan-then-write is one atomic unit and
        # no sequence number can be assigned twice.
        results = []
        errors = []

        def worker(n):
            try:
                results.append(proto.enqueue_say("message {}".format(n)))
            except Exception as e:  # pragma: no cover - failure path only
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert sorted(results) == list(range(1, 11))
        say_re = re.compile(r"^say_(\d+)\.txt$")
        written = sorted(
            int(say_re.match(n).group(1))
            for n in os.listdir(proto.plugins_dir)
            if say_re.match(n)
        )
        assert written == list(range(1, 11))


class TestWorkshopUnsubscribeRequest:
    """workshop-ingest-hardening.md §1.4 -- what a quarantine writes so Steam cannot
    silently re-download the item it just rejected."""

    def test_one_id_is_written_one_per_line(self, proto):
        proto.request_workshop_unsubscribe(["3766917302"])
        assert proto.read_text("workshop_unsubscribe_request.txt") == "3766917302"
        assert proto.read_lines("workshop_unsubscribe_request.txt") == ["3766917302"]

    def test_several_ids_keep_their_order(self, proto):
        proto.request_workshop_unsubscribe([111, "222", 333])
        assert proto.read_lines("workshop_unsubscribe_request.txt") == ["111", "222", "333"]

    def test_a_non_numeric_id_is_refused(self, proto):
        # Unlike the download request there is no result file to carry a `bad_id` back,
        # so junk would be dropped silently by the plugin. Refuse it here instead.
        with pytest.raises(ValueError):
            proto.request_workshop_unsubscribe(["../../etc/passwd"])
        assert not proto.exists("workshop_unsubscribe_request.txt")

    def test_an_empty_request_is_refused(self, proto):
        with pytest.raises(ValueError):
            proto.request_workshop_unsubscribe([])

    def test_more_ids_than_the_plugin_reads_is_refused(self, proto):
        with pytest.raises(ValueError):
            proto.request_workshop_unsubscribe([str(i) for i in range(100, 100 + 17)])


class TestWorkshopDownloadClaim:
    """§9.4 -- control-plane-internal arbitration between the blocking CLI and the
    orchestrator's auto-ingest, which both poll the single result file."""

    def test_claiming_then_releasing_round_trips(self, proto):
        proto.claim_workshop_downloads(["111", "222"])
        ids, mtime = proto.read_workshop_download_claim()
        assert ids == ["111", "222"]
        assert mtime is not None
        assert proto.release_workshop_downloads() is True
        assert proto.read_workshop_download_claim() == ([], None)

    def test_reclaiming_refreshes_the_mtime(self, proto):
        proto.claim_workshop_downloads(["111"])
        _, first = proto.read_workshop_download_claim()
        os.utime(proto.path("workshop_download_claim.txt"), (first - 100, first - 100))
        proto.claim_workshop_downloads(["111"])
        _, second = proto.read_workshop_download_claim()
        assert second > first - 100

    def test_the_claim_file_is_control_plane_writable_and_the_busy_marker_is_not(self):
        assert "workshop_download_claim.txt" in WRITABLE
        assert "workshop_unsubscribe_request.txt" in WRITABLE
        assert "workshop_download_busy.txt" in READ_ONLY


class TestRootOwnershipHandover:
    """§8.3 -- `docker compose exec` defaults to root, so anything the control plane
    creates while root must be handed to the user that owns the plugins directory, or the
    plugin (botuser) cannot write it afterwards. Reproduced live 2026-09-03: /track
    answered "Access denied" until a human ran chown."""

    @staticmethod
    def pretend_dir_is_botusers(proto, monkeypatch):
        """os.stat(plugins_dir) reports uid/gid 1000, while every OTHER stat (the ones
        makedirs and open do internally) keeps returning the real thing."""
        os.makedirs(proto.plugins_dir, exist_ok=True)
        real_stat = os.stat

        class OwnedByBotuser:
            def __init__(self, real):
                self._real = real
                self.st_uid = 1000
                self.st_gid = 1000

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(os, "stat", lambda p, *a, **k: (
            OwnedByBotuser(real_stat(p, *a, **k)) if str(p) == proto.plugins_dir
            else real_stat(p, *a, **k)))

    @pytest.fixture
    def as_root(self, proto, monkeypatch):
        """Pretend to be root over a botuser-owned plugins dir, and record chowns."""
        self.pretend_dir_is_botusers(proto, monkeypatch)
        calls = []
        monkeypatch.setattr(os, "chown", lambda p, uid, gid: calls.append((str(p), uid, gid)))
        return calls

    def test_a_written_file_is_chowned_before_the_atomic_rename(self, proto, as_root):
        proto.set_lobby_name("Test Lobby")
        chowned = [c for c in as_root if "lobby_name.txt" in c[0]]
        assert chowned, as_root
        # The TEMP file, not the target: the rename then publishes an already-correctly
        # owned file, so the live file is never momentarily root-owned.
        assert chowned[0][0].endswith(".tmp.{}".format(os.getpid()))
        assert chowned[0][1:] == (1000, 1000)

    def test_the_lock_file_is_chowned_too(self, proto, as_root):
        """One layer deeper than the reported bug: .control.lock is created 0o666 BEFORE
        umask, so a root-created one lands 0644 root:root and every later botuser writer
        fails its os.open with EACCES."""
        proto.set_lobby_name("Test Lobby")
        assert any(c[0].endswith(".control.lock") and c[1:] == (1000, 1000) for c in as_root)

    def test_nothing_is_chowned_when_we_are_not_root(self, proto, monkeypatch):
        calls = []
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.setattr(os, "chown", lambda p, uid, gid: calls.append((p, uid, gid)))
        proto.set_lobby_name("Test Lobby")
        assert calls == []

    def test_a_failing_chown_does_not_fail_the_write(self, proto, monkeypatch):
        self.pretend_dir_is_botusers(proto, monkeypatch)

        def boom(*args):
            raise OSError("not permitted")

        monkeypatch.setattr(os, "chown", boom)
        proto.set_lobby_name("Test Lobby")
        assert proto.read_text("lobby_name.txt") == "Test Lobby"


class TestConditionalResultConsume:
    """`accept` makes consuming the single-slot result file conditional AND atomic
    (workshop-ingest-hardening review, cluster B + finding 6, 2026-09-04).

    Two consumers poll this one file -- the blocking CLI and the orchestrator's
    auto-ingest -- and each must leave the other's outcomes alone. Checking one read and
    consuming another is not enough: the record taken need not be the record that passed
    the check, and either way a wrongly-taken result is a download nobody ever ingests."""

    def write_result(self, proto, published_id):
        os.makedirs(proto.plugins_dir, exist_ok=True)
        with open(proto.path("workshop_download_result.txt"), "w") as fh:
            fh.write("{}|ok|\n".format(published_id))

    def test_an_accepted_record_is_consumed(self, proto):
        self.write_result(proto, "111")
        record = proto.consume_workshop_download_result(
            accept=lambda r: r["published_id"] == "111")
        assert record["published_id"] == "111"
        assert not proto.exists("workshop_download_result.txt")

    def test_a_rejected_record_is_left_exactly_where_it_was(self, proto):
        self.write_result(proto, "999")
        assert proto.consume_workshop_download_result(
            accept=lambda r: r["published_id"] == "111") is None
        assert proto.read_workshop_download_result()["published_id"] == "999"

    def test_no_predicate_still_consumes_anything(self, proto):
        self.write_result(proto, "999")
        assert proto.consume_workshop_download_result()["published_id"] == "999"
        assert not proto.exists("workshop_download_result.txt")

    def test_the_record_returned_is_the_record_the_predicate_saw(self, proto):
        """The plugin can rewrite the file between the peek and the consume. Whatever
        comes back must be what was checked -- otherwise the auto-ingest can start a batch
        on a result the CLI is blocking on, and the CLI times out for nothing."""
        self.write_result(proto, "111")
        seen = []

        def accept(record):
            seen.append(record["published_id"])
            if len(seen) == 1:
                # Simulate the plugin's next tick landing right here, between the peek
                # and the rename that claims the bytes.
                self.write_result(proto, "999")
            return record["published_id"] == "111"

        record = proto.consume_workshop_download_result(accept=accept)
        assert seen == ["111", "999"], (
            "the record actually taken must be re-checked, not assumed to be the peeked one")
        assert record is None, "it took a record the predicate never approved"
        assert proto.read_workshop_download_result()["published_id"] == "999", (
            "the outcome that arrived mid-consume was destroyed")

    def test_a_half_written_line_is_never_consumed(self, proto):
        os.makedirs(proto.plugins_dir, exist_ok=True)
        with open(proto.path("workshop_download_result.txt"), "w") as fh:
            fh.write("111|o")
        assert proto.consume_workshop_download_result(accept=lambda r: True) is None
        assert proto.exists("workshop_download_result.txt")

    def test_no_staging_file_is_left_behind(self, proto):
        self.write_result(proto, "111")
        proto.consume_workshop_download_result()
        leftovers = [n for n in os.listdir(proto.plugins_dir) if ".consuming." in n]
        assert leftovers == []
