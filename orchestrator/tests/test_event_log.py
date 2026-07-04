import json
import os
import re
from datetime import datetime, timezone

import pytest

import event_log
from event_log import EventLogger, resolve_log_dir, daily_filename, format_ts

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _fixed_clock(dt):
    return lambda: dt


def _read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if line]


class TestTimestampFormat:
    def test_iso8601_utc_second_precision_z_suffix(self):
        dt = datetime(2026, 7, 4, 14, 23, 1, 500000, tzinfo=timezone.utc)
        assert format_ts(dt) == "2026-07-04T14:23:01Z"
        assert TS_RE.match(format_ts(dt))

    def test_naive_datetime_is_treated_as_utc(self):
        # No tzinfo -> formatted as-is (assumed UTC), no crash.
        assert format_ts(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05Z"

    def test_non_utc_tz_is_converted_to_utc(self):
        from datetime import timedelta
        tz_plus2 = timezone(timedelta(hours=2))
        dt = datetime(2026, 7, 4, 16, 0, 0, tzinfo=tz_plus2)  # == 14:00 UTC
        assert format_ts(dt) == "2026-07-04T14:00:00Z"


class TestEmitSerialization:
    def test_one_object_per_line_valid_json(self, tmp_path):
        logger = EventLogger(str(tmp_path))
        logger.emit("game_start", pid=1234)
        logger.emit("shutdown", reason="maintenance")

        path = logger.current_path()
        lines = _read_lines(path)
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)  # each line independently parseable
            assert isinstance(obj, dict)

    def test_envelope_fields_present_and_valued(self, tmp_path):
        logger = EventLogger(str(tmp_path))
        rec = logger.emit("game_start", pid=42)
        assert rec["source"] == "orchestrator"
        assert rec["event"] == "game_start"
        assert TS_RE.match(rec["ts"])
        # order: ts, source, event first
        assert list(rec.keys())[:3] == ["ts", "source", "event"]

    def test_written_record_matches_returned_record(self, tmp_path):
        logger = EventLogger(str(tmp_path))
        rec = logger.emit("error", message="boom")
        on_disk = json.loads(_read_lines(logger.current_path())[0])
        assert on_disk == rec

    def test_none_optional_fields_are_omitted(self, tmp_path):
        logger = EventLogger(str(tmp_path))
        rec = logger.game_start(99, playlist=None, width=None, height=None)
        assert "playlist" not in rec
        assert "width" not in rec
        assert rec["pid"] == 99

    def test_custom_source(self, tmp_path):
        logger = EventLogger(str(tmp_path), source="plugin")
        rec = logger.emit("scene_change")
        assert rec["source"] == "plugin"

    def test_append_does_not_overwrite(self, tmp_path):
        logger = EventLogger(str(tmp_path))
        for i in range(5):
            logger.emit("game_start", pid=i)
        assert len(_read_lines(logger.current_path())) == 5

    def test_directory_created_on_demand(self, tmp_path):
        target = tmp_path / "nested" / "logs"
        logger = EventLogger(str(target))
        logger.emit("game_start", pid=1)
        assert target.is_dir()

    def test_emit_never_raises_on_io_error(self, tmp_path, monkeypatch, capsys):
        logger = EventLogger(str(tmp_path))

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", boom)
        rec = logger.emit("game_start", pid=1)  # must not raise
        assert rec["event"] == "game_start"
        assert "WARNING" in capsys.readouterr().out


class TestDailyFilenameRotation:
    def test_filename_matches_utc_date(self):
        assert daily_filename(datetime(2026, 7, 4, tzinfo=timezone.utc)) == "bot-2026-07-04.jsonl"

    def test_new_file_when_date_rolls(self, tmp_path):
        day1 = datetime(2026, 7, 4, 23, 59, 59, tzinfo=timezone.utc)
        day2 = datetime(2026, 7, 5, 0, 0, 5, tzinfo=timezone.utc)

        logger = EventLogger(str(tmp_path), clock=_fixed_clock(day1))
        logger.emit("game_start", pid=1)

        logger._clock = _fixed_clock(day2)
        logger.emit("game_start", pid=2)

        assert (tmp_path / "bot-2026-07-04.jsonl").exists()
        assert (tmp_path / "bot-2026-07-05.jsonl").exists()
        # each day's file holds only that day's event
        assert len(_read_lines(str(tmp_path / "bot-2026-07-04.jsonl"))) == 1
        assert len(_read_lines(str(tmp_path / "bot-2026-07-05.jsonl"))) == 1

    def test_ts_and_filename_derive_from_same_instant(self, tmp_path):
        day = datetime(2026, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        logger = EventLogger(str(tmp_path), clock=_fixed_clock(day))
        rec = logger.emit("game_start", pid=1)
        path = logger.current_path(day)
        assert os.path.basename(path) == "bot-2026-12-31.jsonl"
        assert rec["ts"].startswith("2026-12-31")


class TestResolveLogDir:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FPV_LOG_DIR", "/mnt/volume/logs")
        assert resolve_log_dir({"log_dir": "other"}, str(tmp_path)) == "/mnt/volume/logs"

    def test_config_absolute_used_as_is(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FPV_LOG_DIR", raising=False)
        assert resolve_log_dir({"log_dir": "/var/log/fpv"}, str(tmp_path)) == "/var/log/fpv"

    def test_config_relative_resolved_against_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FPV_LOG_DIR", raising=False)
        got = resolve_log_dir({"log_dir": "mylogs"}, str(tmp_path))
        assert got == os.path.join(str(tmp_path), "mylogs")

    def test_default_is_repo_logs(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FPV_LOG_DIR", raising=False)
        assert resolve_log_dir({}, str(tmp_path)) == os.path.join(str(tmp_path), "logs")
        assert resolve_log_dir(None, str(tmp_path)) == os.path.join(str(tmp_path), "logs")

    def test_default_uses_repo_root_when_no_project_dir(self, monkeypatch):
        monkeypatch.delenv("FPV_LOG_DIR", raising=False)
        got = resolve_log_dir({})
        assert got == os.path.join(event_log.repo_root(), "logs")


class TestTypedHelpers:
    def test_game_start_required_fields(self, tmp_path):
        rec = EventLogger(str(tmp_path)).game_start(555, playlist="all", width=640, height=480)
        assert rec["event"] == "game_start"
        assert rec["pid"] == 555
        assert rec["playlist"] == "all"
        assert rec["width"] == 640 and rec["height"] == 480

    def test_playlist_resolved_required_fields(self, tmp_path):
        rec = EventLogger(str(tmp_path)).playlist_resolved(
            "sprint", 12, shuffle=True, dropped_missing=1, dropped_mode=2, fallback=False)
        assert rec["event"] == "playlist_resolved"
        assert rec["playlist"] == "sprint"
        assert rec["track_count"] == 12
        assert rec["shuffle"] is True
        assert rec["dropped_missing"] == 1 and rec["dropped_mode"] == 2
        assert rec["fallback"] is False

    def test_playlist_change_uses_from_to_keys(self, tmp_path):
        rec = EventLogger(str(tmp_path)).playlist_change("a", "b")
        assert rec["event"] == "playlist_change"
        assert rec["from"] == "a"
        assert rec["to"] == "b"

    def test_orchestrator_start_required_field(self, tmp_path):
        rec = EventLogger(str(tmp_path)).orchestrator_start(600, playlist="p", gui=False)
        assert rec["event"] == "orchestrator_start"
        assert rec["interval"] == 600

    def test_shutdown_required_field(self, tmp_path):
        rec = EventLogger(str(tmp_path)).shutdown("maintenance")
        assert rec["event"] == "shutdown"
        assert rec["reason"] == "maintenance"

    def test_error_required_field(self, tmp_path):
        rec = EventLogger(str(tmp_path)).error("kaboom", context="gather_tracks", playlist="p")
        assert rec["event"] == "error"
        assert rec["message"] == "kaboom"
        assert rec["context"] == "gather_tracks"
        assert rec["playlist"] == "p"

    def test_error_optional_fields_omitted_when_none(self, tmp_path):
        rec = EventLogger(str(tmp_path)).error("kaboom")
        assert rec["message"] == "kaboom"
        assert "context" not in rec and "playlist" not in rec


class TestSharedFileCoWriting:
    def test_orchestrator_and_plugin_share_one_file_distinguished_by_source(self, tmp_path):
        day = datetime(2026, 7, 4, 10, 0, 0, tzinfo=timezone.utc)
        orch = EventLogger(str(tmp_path), source="orchestrator", clock=_fixed_clock(day))
        plug = EventLogger(str(tmp_path), source="plugin", clock=_fixed_clock(day))

        orch.game_start(1)
        plug.emit("scene_change", **{"from": "MainMenu", "to": "MultiplayerMenu"})
        orch.shutdown("keyboard_interrupt")

        # All three land in the same daily file, still one-object-per-line.
        lines = _read_lines(str(tmp_path / "bot-2026-07-04.jsonl"))
        assert len(lines) == 3
        sources = [json.loads(line)["source"] for line in lines]
        assert sources == ["orchestrator", "plugin", "orchestrator"]
