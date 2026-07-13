"""Unit tests for scenario_harness's pure/deterministic pieces only. Everything that
shells out to `sudo -u fpv_bot` (deploys, launches, log reads) is out of scope here --
that glue is covered by the live scenario runs themselves (see automated-testing.md,
layer 2/3). wait_for_log_pattern's matching/timeout logic is exercised by monkeypatching
its single I/O seam (read_fpv_text), never by spawning a subprocess.
"""

import hashlib

import scenario_harness as sh
from scenario_harness import local_md5, kill_process, wait_for_log_pattern


class TestWaitForLogPattern:
    def test_returns_the_matching_line(self, monkeypatch):
        text = "line one\n[AutoLobbyPlugin:EVENT] {\"event\":\"chat_response\"}\nline three\n"
        monkeypatch.setattr(sh, "read_fpv_text", lambda path: text)
        got = wait_for_log_pattern("/fake/log", r"chat_response", timeout_s=1, poll=0.01)
        assert got == '[AutoLobbyPlugin:EVENT] {"event":"chat_response"}'

    def test_pattern_is_a_regex_searched_per_line(self, monkeypatch):
        monkeypatch.setattr(sh, "read_fpv_text", lambda path: "abc room_entered xyz\n")
        got = wait_for_log_pattern("/fake/log", r"room_\w+", timeout_s=1, poll=0.01)
        assert got == "abc room_entered xyz"

    def test_no_match_times_out_with_none(self, monkeypatch):
        monkeypatch.setattr(sh, "read_fpv_text", lambda path: "nothing relevant here\n")
        assert wait_for_log_pattern("/fake/log", r"never_appears", timeout_s=0.05, poll=0.01) is None

    def test_tolerates_missing_file_until_timeout(self, monkeypatch):
        # read_fpv_text returns None while the log doesn't exist yet (fresh launch);
        # the poller must ride that out quietly instead of crashing.
        monkeypatch.setattr(sh, "read_fpv_text", lambda path: None)
        assert wait_for_log_pattern("/fake/log", r"anything", timeout_s=0.05, poll=0.01) is None

    def test_first_matching_line_wins_over_later_ones(self, monkeypatch):
        monkeypatch.setattr(sh, "read_fpv_text",
                            lambda path: "match early\nmatch late\n")
        got = wait_for_log_pattern("/fake/log", r"match", timeout_s=1, poll=0.01)
        assert got == "match early"


class TestLocalMd5:
    def test_matches_hashlib_over_file_bytes(self, tmp_path):
        payload = b"\x00\x01binary dll bytes\xff" * 100
        path = tmp_path / "plugin.dll"
        path.write_bytes(payload)
        assert local_md5(str(path)) == hashlib.md5(payload).hexdigest()

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        assert local_md5(str(path)) == hashlib.md5(b"").hexdigest()


class TestKillProcess:
    def test_none_is_a_noop(self):
        kill_process(None)  # must not raise

    def test_already_exited_process_is_not_terminated(self):
        class FakeProc:
            def __init__(self):
                self.terminated = False

            def poll(self):
                return 0  # already exited

            def terminate(self):
                self.terminated = True

        proc = FakeProc()
        kill_process(proc)
        assert proc.terminated is False

    def test_running_process_gets_terminate_then_wait(self):
        calls = []

        class FakeProc:
            def poll(self):
                return None  # still running

            def terminate(self):
                calls.append("terminate")

            def wait(self, timeout=None):
                calls.append("wait")

        kill_process(FakeProc())
        assert calls == ["terminate", "wait"]

    def test_terminate_failure_falls_back_to_kill(self):
        calls = []

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                raise OSError("no such process")

            def kill(self):
                calls.append("kill")

        kill_process(FakeProc())  # must not raise
        assert calls == ["kill"]
