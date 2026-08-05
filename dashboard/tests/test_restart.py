"""Tests for dashboard.control.restart (decision D3's restart-bot button).

The behaviour that matters most is the refusal: this repo is developed on a machine that
runs a bot container next to the worktrees, and the button must be inert until an
operator deliberately enables it and names what to restart.
"""

import subprocess

import pytest

from dashboard.control import restart
from dashboard.control.settings import DashboardSettings


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def enabled_settings():
    return DashboardSettings(token="t", allow_container_restart=True,
                             restart_command=["docker", "restart", "fpv-bot"])


class TestRefusals:
    def test_disabled_by_default(self):
        with pytest.raises(restart.RestartUnavailableError) as exc:
            restart.restart_bot(DashboardSettings(token="t"))
        assert "disabled" in str(exc.value)

    def test_enabled_without_a_command_is_still_refused(self):
        settings = DashboardSettings(token="t", allow_container_restart=True)
        with pytest.raises(restart.RestartUnavailableError) as exc:
            restart.restart_bot(settings)
        assert "no command is configured" in str(exc.value)

    def test_missing_binary_is_reported_before_running_anything(self, enabled_settings, monkeypatch):
        monkeypatch.setattr(restart.shutil, "which", lambda name: None)
        called = []
        with pytest.raises(restart.RestartUnavailableError) as exc:
            restart.restart_bot(enabled_settings, runner=lambda *a, **kw: called.append(a))
        assert "docker" in str(exc.value)
        assert called == []


class TestExecution:
    def test_runs_the_configured_command(self, enabled_settings, monkeypatch):
        monkeypatch.setattr(restart.shutil, "which", lambda name: "/usr/bin/" + name)
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            return FakeCompleted(stdout="fpv-bot\n")

        result = restart.restart_bot(enabled_settings, runner=runner)
        assert seen["command"] == ["docker", "restart", "fpv-bot"]
        assert result["stdout"] == "fpv-bot"

    def test_nonzero_exit_raises_with_the_output(self, enabled_settings, monkeypatch):
        monkeypatch.setattr(restart.shutil, "which", lambda name: "/usr/bin/docker")
        runner = lambda command, **kw: FakeCompleted(returncode=1, stderr="No such container\n")
        with pytest.raises(restart.RestartFailedError) as exc:
            restart.restart_bot(enabled_settings, runner=runner)
        assert "No such container" in str(exc.value)

    def test_timeout_is_reported_not_propagated(self, enabled_settings, monkeypatch):
        monkeypatch.setattr(restart.shutil, "which", lambda name: "/usr/bin/docker")

        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 60)

        with pytest.raises(restart.RestartFailedError) as exc:
            restart.restart_bot(enabled_settings, runner=runner)
        assert "timed out" in str(exc.value)

    def test_oserror_is_reported_not_propagated(self, enabled_settings, monkeypatch):
        monkeypatch.setattr(restart.shutil, "which", lambda name: "/usr/bin/docker")

        def runner(command, **kwargs):
            raise OSError("permission denied")

        with pytest.raises(restart.RestartFailedError):
            restart.restart_bot(enabled_settings, runner=runner)
