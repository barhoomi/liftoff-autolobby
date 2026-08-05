"""Tests for dashboard.control.events — reading and tailing the shared JSONL log.

The cases that matter are the ones a naive tail gets wrong against a file two processes
are appending to: a torn final line, a day rollover at midnight, a file replaced
underneath the cursor, and lines from a schema this code has never seen.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from dashboard.control import events as events_mod

DAY = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def write_events(path, records):
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def event(name, ts="2026-08-05T12:00:00Z", **fields):
    record = {"ts": ts, "source": "plugin", "event": name}
    record.update(fields)
    return record


class TestFilenames:
    def test_daily_filename_uses_the_utc_date(self):
        assert events_mod.daily_filename(DAY) == "bot-2026-08-05.jsonl"

    def test_daily_path_joins_the_log_dir(self, tmp_path):
        assert events_mod.daily_path(str(tmp_path), DAY) == str(tmp_path / "bot-2026-08-05.jsonl")

    def test_list_daily_files_is_newest_first_and_ignores_strangers(self, tmp_path):
        for name in ("bot-2026-08-03.jsonl", "bot-2026-08-05.jsonl", "notes.txt",
                     "bot-oops.jsonl"):
            (tmp_path / name).write_text("")
        names = [f["name"] for f in events_mod.list_daily_files(str(tmp_path))]
        assert names == ["bot-2026-08-05.jsonl", "bot-2026-08-03.jsonl"]

    def test_list_daily_files_of_a_missing_dir_is_empty(self, tmp_path):
        assert events_mod.list_daily_files(str(tmp_path / "nope")) == []


class TestParsing:
    def test_blank_lines_are_dropped(self):
        assert events_mod.parse_line("   ") is None

    def test_unknown_fields_survive_untouched(self):
        parsed = events_mod.parse_line(json.dumps(
            {"ts": "x", "source": "plugin", "event": "lap_time", "seconds": 42.5}))
        assert parsed["event"] == "lap_time" and parsed["seconds"] == 42.5

    def test_corrupt_line_is_surfaced_not_swallowed(self):
        parsed = events_mod.parse_line('{"ts": "x", "event": ')
        assert parsed["event"] == "_unparseable"
        assert parsed["raw"].startswith('{"ts"')

    def test_non_object_json_is_also_unparseable(self):
        assert events_mod.parse_line("[1, 2, 3]")["event"] == "_unparseable"


class TestReadRecent:
    def test_reads_todays_file(self, tmp_path):
        write_events(str(tmp_path / "bot-2026-08-05.jsonl"), [event("chat"), event("rotation")])
        events = events_mod.read_recent(str(tmp_path), now=DAY)
        assert [e["event"] for e in events] == ["chat", "rotation"]

    def test_walks_back_to_yesterday_when_today_is_thin(self, tmp_path):
        write_events(str(tmp_path / "bot-2026-08-04.jsonl"), [event("rotation")])
        write_events(str(tmp_path / "bot-2026-08-05.jsonl"), [event("chat")])
        events = events_mod.read_recent(str(tmp_path), limit=10, now=DAY)
        # Yesterday's events come first: the merged view stays chronological.
        assert [e["event"] for e in events] == ["rotation", "chat"]

    def test_stops_walking_back_once_the_limit_is_met(self, tmp_path):
        write_events(str(tmp_path / "bot-2026-08-04.jsonl"), [event("rotation")])
        write_events(str(tmp_path / "bot-2026-08-05.jsonl"), [event("chat"), event("chat")])
        events = events_mod.read_recent(str(tmp_path), limit=2, now=DAY)
        assert [e["event"] for e in events] == ["chat", "chat"]

    def test_zero_limit_returns_nothing_not_everything(self, tmp_path):
        # events[-0:] is the whole list; the SSE stream asks for backlog=0 when the
        # operator only wants new lines, and must not get the entire day instead.
        write_events(str(tmp_path / "bot-2026-08-05.jsonl"), [event("chat"), event("chat")])
        assert events_mod.read_recent(str(tmp_path), limit=0, now=DAY) == []

    def test_missing_dir_or_none_dir_is_empty(self, tmp_path):
        assert events_mod.read_recent(None) == []
        assert events_mod.read_recent(str(tmp_path / "nope"), now=DAY) == []

    def test_events_are_tagged_with_their_source_file(self, tmp_path):
        write_events(str(tmp_path / "bot-2026-08-05.jsonl"), [event("chat")])
        assert events_mod.read_recent(str(tmp_path), now=DAY)[0]["_file"] == "bot-2026-08-05.jsonl"


class TestEventTail:
    def _tail(self, tmp_path, clock_day=DAY):
        holder = {"now": clock_day}
        tail = events_mod.EventTail(str(tmp_path), clock=lambda: holder["now"])
        return tail, holder

    def test_seek_to_end_skips_existing_content(self, tmp_path):
        path = tmp_path / "bot-2026-08-05.jsonl"
        write_events(str(path), [event("chat")])
        tail, _ = self._tail(tmp_path)
        tail.seek_to_end()
        assert tail.poll() == []

    def test_new_lines_are_returned_once(self, tmp_path):
        path = tmp_path / "bot-2026-08-05.jsonl"
        write_events(str(path), [event("chat")])
        tail, _ = self._tail(tmp_path)
        tail.seek_to_end()
        write_events(str(path), [event("rotation"), event("player_join")])
        assert [e["event"] for e in tail.poll()] == ["rotation", "player_join"]
        assert tail.poll() == []

    def test_a_torn_final_line_is_withheld_until_complete(self, tmp_path):
        path = tmp_path / "bot-2026-08-05.jsonl"
        path.write_text("")
        tail, _ = self._tail(tmp_path)
        tail.seek_to_end()
        with open(path, "a") as f:
            f.write('{"ts": "2026-08-05T12:00:00Z", "source": "plugin", "event": "rot')
        assert tail.poll() == []          # partial: nothing emitted, nothing consumed
        with open(path, "a") as f:
            f.write('ation", "track": "T"}\n')
        assert [e["event"] for e in tail.poll()] == ["rotation"]

    def test_day_rollover_switches_files_without_missing_a_line(self, tmp_path):
        today = tmp_path / "bot-2026-08-05.jsonl"
        write_events(str(today), [event("chat")])
        tail, holder = self._tail(tmp_path)
        tail.seek_to_end()
        # A line lands in the old file, then midnight passes and the writer moves on.
        write_events(str(today), [event("rotation")])
        tomorrow = tmp_path / "bot-2026-08-06.jsonl"
        write_events(str(tomorrow), [event("room_entered")])
        holder["now"] = DAY + timedelta(days=1)
        assert [e["event"] for e in tail.poll()] == ["rotation", "room_entered"]
        assert tail.path.endswith("bot-2026-08-06.jsonl")

    def test_a_truncated_file_is_reread_from_the_start(self, tmp_path):
        path = tmp_path / "bot-2026-08-05.jsonl"
        write_events(str(path), [event("chat"), event("chat")])
        tail, _ = self._tail(tmp_path)
        tail.seek_to_end()
        path.write_text("")
        write_events(str(path), [event("rotation")])
        assert [e["event"] for e in tail.poll()] == ["rotation"]

    def test_missing_file_polls_empty_until_it_appears(self, tmp_path):
        tail, _ = self._tail(tmp_path)
        tail.seek_to_end()
        assert tail.poll() == []
        write_events(str(tmp_path / "bot-2026-08-05.jsonl"), [event("chat")])
        assert [e["event"] for e in tail.poll()] == ["chat"]

    def test_utf8_payloads_do_not_desynchronize_the_offset(self, tmp_path):
        # Offsets are byte offsets; a multibyte nickname would shift them if the code
        # measured characters instead (the next poll would then re-emit a partial line).
        path = tmp_path / "bot-2026-08-05.jsonl"
        path.write_text("")
        tail, _ = self._tail(tmp_path)
        tail.seek_to_end()
        write_events(str(path), [event("chat", player="日本のパイロット", msg="привет")])
        assert len(tail.poll()) == 1
        write_events(str(path), [event("chat", msg="second")])
        second = tail.poll()
        assert len(second) == 1 and second[0]["msg"] == "second"

    def test_no_log_dir_is_a_quiet_no_op(self):
        assert events_mod.EventTail(None).poll() == []


def test_daily_files_survive_a_stat_race(tmp_path):
    # Listing must not explode if a file vanishes between listdir and stat.
    (tmp_path / "bot-2026-08-05.jsonl").write_text("")
    real_stat = os.stat

    def flaky(path, *a, **kw):
        if str(path).endswith("bot-2026-08-05.jsonl"):
            raise OSError("gone")
        return real_stat(path, *a, **kw)

    os.stat = flaky
    try:
        assert events_mod.list_daily_files(str(tmp_path)) == []
    finally:
        os.stat = real_stat
