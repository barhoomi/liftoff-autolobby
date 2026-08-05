"""Dashboard configuration (decision D2: one shared token, no user management).

Sources, first hit wins per key:

1. environment variables (``FPV_DASHBOARD_*``) — the Docker/systemd hook, and the only
   place a secret belongs if the repo checkout is shared;
2. a ``"dashboard"`` object inside ``config/lobby_config.json`` — the "set in config"
   half of D2, next to every other runtime setting the operator already edits;
3. built-in defaults.

The token has no default on purpose. A dashboard that can write ``tracks_to_rotate.txt``
and schedule a game shutdown must not come up unauthenticated because someone forgot a
config key — ``python -m dashboard`` refuses to start without one (fail closed, the same
posture ``admin_ids.txt`` takes for chat commands).
"""

import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
# How often the SSE stream polls the JSONL file for new lines. The producers are
# human-paced (a few events a minute), so this trades nothing for a cheap loop.
DEFAULT_POLL_INTERVAL = 1.0


def _env_flag(env, name, default=False):
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class DashboardSettings:
    """Resolved dashboard configuration. Plain object (no pydantic) so the control
    package keeps its "importable without the web extras" property."""

    def __init__(self, token=None, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 poll_interval=DEFAULT_POLL_INTERVAL, allow_container_restart=False,
                 restart_command=None, project_dir=None, config=None):
        self.token = token
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        # D3's restart-bot button. Defaults to DISABLED and stays that way unless the
        # operator both flips the flag and names a command: on a development machine the
        # container running next to a worktree is somebody's live session, and a
        # dashboard that can restart it by default is a foot-gun, not a feature.
        self.allow_container_restart = allow_container_restart
        self.restart_command = list(restart_command) if restart_command else []
        self.project_dir = project_dir
        self.config = config or {}

    @property
    def restart_available(self):
        return bool(self.allow_container_restart and self.restart_command)

    def public_dict(self):
        """Settings safe to hand to the browser (never the token)."""
        return {
            "host": self.host,
            "port": self.port,
            "poll_interval": self.poll_interval,
            "restart_available": self.restart_available,
            "restart_command": self.restart_command if self.restart_available else None,
        }


def load_settings(project_dir=None, env=None, config=None):
    from .paths import load_lobby_config_or_empty, repo_root

    env = os.environ if env is None else env
    project_dir = project_dir or repo_root()
    if config is None:
        config = load_lobby_config_or_empty(project_dir)
    section = config.get("dashboard") or {}

    token = env.get("FPV_DASHBOARD_TOKEN") or section.get("token") or None

    host = env.get("FPV_DASHBOARD_HOST") or section.get("host") or DEFAULT_HOST
    try:
        port = int(env.get("FPV_DASHBOARD_PORT") or section.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        poll_interval = float(env.get("FPV_DASHBOARD_POLL_INTERVAL")
                              or section.get("poll_interval") or DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        poll_interval = DEFAULT_POLL_INTERVAL

    allow_restart = _env_flag(env, "FPV_DASHBOARD_ALLOW_RESTART",
                              bool(section.get("allow_container_restart", False)))

    restart_command = section.get("restart_command")
    if env.get("FPV_DASHBOARD_RESTART_COMMAND"):
        restart_command = env["FPV_DASHBOARD_RESTART_COMMAND"].split()
    elif not restart_command:
        container = env.get("FPV_BOT_CONTAINER") or section.get("container_name")
        restart_command = ["docker", "restart", container] if container else []

    return DashboardSettings(
        token=token, host=host, port=port, poll_interval=poll_interval,
        allow_container_restart=allow_restart, restart_command=restart_command,
        project_dir=project_dir, config=config,
    )
