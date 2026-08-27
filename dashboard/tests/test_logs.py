"""Tests for dashboard.control.logs — the raw log catalogue and tailer.

Two properties are security-relevant rather than merely nice: ids are looked up in a
catalogue (so no request can name a path), and files that exist but are unreadable are
reported as such instead of disappearing — on a bare-metal host the BepInEx and Unity
logs belong to ``fpv_bot`` and a dashboard running as anyone else legitimately cannot
read them.
"""

import os

import pytest

from dashboard.control import logs as logs_mod


@pytest.fixture
def bot_tree(tmp_path, monkeypatch):
    """A fake install: <root>/Liftoff/{Liftoff.x86_64,BepInEx/LogOutput.log} plus the
    Unity Player.log at the Docker-shaped location (home == the game dir's parent)."""
    for var in ("FPV_LOG_DIR", "FPV_PLUGINS_DIR", "FPV_GAME_DIR", "FPV_PLAYER_LOG"):
        monkeypatch.delenv(var, raising=False)
    game_dir = tmp_path / "Liftoff"
    (game_dir / "BepInEx").mkdir(parents=True)
    (game_dir / "Liftoff.x86_64").write_text("")
    (game_dir / "BepInEx" / "LogOutput.log").write_text("plugin line 1\nplugin line 2\n")
    player = tmp_path / ".config" / "unity3d" / "LuGus Studios" / "Liftoff" / "Player.log"
    player.parent.mkdir(parents=True)
    player.write_text("unity line\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "bot-2026-08-05.jsonl").write_text('{"ts":"x","source":"plugin","event":"chat"}\n')
    (log_dir / "bot-2026-08-04.jsonl").write_text("")
    return {"config": {"liftoff_path": str(game_dir / "Liftoff.x86_64")},
            "log_dir": str(log_dir), "game_dir": str(game_dir)}


class TestCatalogue:
    def test_lists_jsonl_dailies_newest_first_then_the_unity_logs(self, bot_tree):
        entries = logs_mod.list_logs(bot_tree["config"], log_dir=bot_tree["log_dir"])
        assert [e["id"] for e in entries] == [
            "jsonl:bot-2026-08-05.jsonl", "jsonl:bot-2026-08-04.jsonl", "bepinex", "player"]

    def test_entries_carry_size_and_readability(self, bot_tree):
        entries = logs_mod.list_logs(bot_tree["config"], log_dir=bot_tree["log_dir"])
        bepinex = next(e for e in entries if e["id"] == "bepinex")
        assert bepinex["exists"] and bepinex["readable"]
        assert bepinex["size"] > 0
        assert bepinex["note"] is None

    def test_missing_file_is_listed_with_a_reason(self, bot_tree):
        os.remove(os.path.join(bot_tree["game_dir"], "BepInEx", "LogOutput.log"))
        entries = logs_mod.list_logs(bot_tree["config"], log_dir=bot_tree["log_dir"])
        bepinex = next(e for e in entries if e["id"] == "bepinex")
        assert bepinex["exists"] is False
        assert bepinex["note"] == "not present"

    def test_unreadable_file_is_listed_with_a_reason(self, bot_tree, monkeypatch):
        monkeypatch.setattr(os, "access", lambda path, mode: not path.endswith("LogOutput.log"))
        entries = logs_mod.list_logs(bot_tree["config"], log_dir=bot_tree["log_dir"])
        bepinex = next(e for e in entries if e["id"] == "bepinex")
        assert bepinex["readable"] is False
        assert "not readable" in bepinex["note"]

    def test_no_game_configured_still_lists_the_jsonl(self, bot_tree):
        entries = logs_mod.list_logs({}, log_dir=bot_tree["log_dir"])
        assert [e["kind"] for e in entries] == ["jsonl", "jsonl"]


class TestLookup:
    def test_known_id_resolves(self, bot_tree):
        entry = logs_mod.find_log("bepinex", bot_tree["config"], log_dir=bot_tree["log_dir"])
        assert entry["path"].endswith("LogOutput.log")

    def test_unknown_and_traversal_ids_resolve_to_nothing(self, bot_tree):
        for bad in ("nope", "../../etc/passwd", "jsonl:../../etc/passwd",
                    "/etc/passwd", "jsonl:bot-9999-99-99.jsonl"):
            assert logs_mod.find_log(bad, bot_tree["config"], log_dir=bot_tree["log_dir"]) is None


class TestTail:
    def test_returns_the_last_n_lines(self, tmp_path):
        path = tmp_path / "big.log"
        path.write_text("\n".join("line {}".format(i) for i in range(500)) + "\n")
        tail = logs_mod.tail_file(str(path), lines=3)
        assert tail.splitlines() == ["line 497", "line 498", "line 499"]

    def test_short_file_returns_everything(self, tmp_path):
        path = tmp_path / "small.log"
        path.write_text("only\n")
        assert logs_mod.tail_file(str(path), lines=100) == "only"

    def test_huge_file_reads_only_the_tail_window(self, tmp_path):
        path = tmp_path / "huge.log"
        path.write_text(("x" * 99 + "\n") * 3000)  # ~300 KB
        tail = logs_mod.tail_file(str(path), lines=5, max_bytes=1000)
        assert len(tail.splitlines()) == 5

    def test_partial_first_line_is_dropped_when_windowing(self, tmp_path):
        path = tmp_path / "w.log"
        path.write_text("AAAAAAAAAA\nBBBBBBBBBB\nCCCCCCCCCC\n")
        tail = logs_mod.tail_file(str(path), lines=10, max_bytes=15)
        assert "AAAAAAAAAA" not in tail
        assert tail.splitlines()[-1] == "CCCCCCCCCC"

    def test_binary_garbage_does_not_raise(self, tmp_path):
        path = tmp_path / "bin.log"
        path.write_bytes(b"\xff\xfe ok\n")
        assert "ok" in logs_mod.tail_file(str(path))

    def test_missing_file_raises_the_typed_error(self, tmp_path):
        with pytest.raises(logs_mod.LogUnreadableError):
            logs_mod.tail_file(str(tmp_path / "nope.log"))
