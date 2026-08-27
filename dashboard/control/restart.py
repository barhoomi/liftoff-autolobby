"""The restart-the-bot action (decision D3).

Restarting means restarting the *container* (or whatever supervises the bot process) —
the dashboard deliberately does not try to kill the game or the orchestrator itself,
because the container entrypoint owns the Steam/Xvfb/BepInEx boot sequence and a
half-restarted stack is worse than a stopped one.

**Disabled by default, on purpose.** The command is only run when the operator has both
flipped ``allow_container_restart`` and named a container/command. A development machine
runs a container next to these worktrees that belongs to somebody's live session, and a
dashboard that could restart it out of the box is a foot-gun rather than a feature. See
the feature doc for the two lines an operator adds in production.
"""

import shutil
import subprocess

DEFAULT_TIMEOUT = 60


class RestartUnavailableError(RuntimeError):
    """The restart action is not enabled/configured on this deployment."""


class RestartFailedError(RuntimeError):
    """The restart command ran and failed; carries its output for the operator."""


def restart_bot(settings, runner=None, timeout=DEFAULT_TIMEOUT):
    """Run the configured restart command. Returns a result dict on success.

    ``runner`` defaults to ``subprocess.run`` but is resolved at CALL time, not bound as
    a default argument: a default-bound reference cannot be replaced by a test, and a
    test that silently falls through to the real ``subprocess.run`` would restart a real
    container on the developer's machine.
    """
    runner = runner or subprocess.run
    if not settings.allow_container_restart:
        raise RestartUnavailableError(
            "Container restart is disabled. Set dashboard.allow_container_restart "
            "(or FPV_DASHBOARD_ALLOW_RESTART=1) to enable it.")
    if not settings.restart_command:
        raise RestartUnavailableError(
            "Container restart is enabled but no command is configured. Set "
            "dashboard.container_name (or FPV_BOT_CONTAINER), or an explicit "
            "dashboard.restart_command.")

    command = list(settings.restart_command)
    if shutil.which(command[0]) is None:
        raise RestartUnavailableError(
            f"'{command[0]}' is not available to the dashboard process. If the dashboard "
            f"runs inside a container, it needs the docker CLI and a mounted docker "
            f"socket to restart a sibling container.")

    try:
        completed = runner(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RestartFailedError(f"'{' '.join(command)}' timed out after {timeout}s.")
    except OSError as e:
        raise RestartFailedError(f"'{' '.join(command)}' could not be executed: {e}")

    if completed.returncode != 0:
        raise RestartFailedError(
            "'{}' exited {}: {}".format(" ".join(command), completed.returncode,
                                        (completed.stderr or completed.stdout or "").strip()))

    return {"command": command, "stdout": (completed.stdout or "").strip()}
