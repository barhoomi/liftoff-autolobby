"""Tests for the event-logging adapter of the control plane
(``dashboard.control.eventlog``). Moved from ``orchestrator/tests/test_run_headless_lobby.py``
with the code under test (bot-dashboard.md, D5); imports are the only change."""

from dashboard.control import eventlog
from dashboard.control.eventlog import NullLogger, make_event_logger


class TestNullLogger:
    def test_any_method_is_a_callable_noop(self):
        logger = NullLogger()
        assert logger.game_start(1234, playlist="all") is None
        assert logger.error("boom", context="anything") is None
        assert logger.some_method_invented_next_year("x", key="y") is None


class TestMakeEventLogger:
    def test_falls_back_to_null_logger_when_module_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eventlog, "EVENT_LOG_AVAILABLE", False)
        logger = make_event_logger({"log_dir": str(tmp_path)}, str(tmp_path))
        assert isinstance(logger, NullLogger)

    def test_builds_working_logger_against_config_log_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FPV_LOG_DIR", raising=False)
        logger = make_event_logger({"log_dir": str(tmp_path)}, str(tmp_path))
        rec = logger.emit("game_start", pid=1)
        assert rec["event"] == "game_start"
        assert any(p.suffix == ".jsonl" for p in tmp_path.iterdir())
