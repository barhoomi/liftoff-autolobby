"""Path/config resolution for the control plane.

Every consumer (orchestrator, dashboard, tests) resolves the repo root, the config
files, the BepInEx plugins directory and the log directory *through this module* — so
there is exactly one answer to "where is X" no matter who is asking (AGENTS.md rule 4).

Notably ``resolve_log_dir`` is NOT re-implemented here: it is re-exported from
``orchestrator/event_log.py``, which is the resolver the orchestrator and (via
``log_dir.txt``) the plugin already agree on.
"""

import json
import os
import sys


def repo_root():
    """Absolute path to the repository root (parent of ``dashboard/``)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bootstrap_orchestrator_on_path():
    """Put ``<repo>/orchestrator`` on sys.path so ``import event_log`` resolves.

    ``orchestrator/`` is its own import root (its modules import each other by bare
    name, e.g. ``run_headless_lobby`` does ``from event_log import ...``), and it is on
    ``pythonpath`` for pytest but not for a plain ``python -m dashboard``. Same
    bootstrap pattern ``orchestrator/gather_tracks.py`` uses in the other direction for
    ``import trackcheck``.
    """
    orchestrator_dir = os.path.join(repo_root(), "orchestrator")
    if orchestrator_dir not in sys.path:
        sys.path.insert(0, orchestrator_dir)
    return orchestrator_dir


_bootstrap_orchestrator_on_path()


def resolve_log_dir(config=None, project_dir=None):
    """The single log-dir resolver (env ``FPV_LOG_DIR`` > ``lobby_config.json:log_dir`` >
    ``<repo>/logs``), delegated to ``orchestrator/event_log.py``.

    Deliberately a delegation and not a re-implementation: the orchestrator resolves the
    directory with that function and hands the answer to the plugin via ``log_dir.txt``,
    so a second derivation here could disagree with the file the plugin is reading.
    Returns ``None`` when ``event_log`` itself is unavailable (see ``eventlog.py`` for
    why that import is defensive) — callers treat that as "structured logging is off"
    rather than guessing a directory.
    """
    from .eventlog import EVENT_LOG_AVAILABLE
    from .eventlog import resolve_log_dir as _impl

    if not EVENT_LOG_AVAILABLE:
        return None
    return _impl(config, project_dir)


def config_dir(project_dir=None):
    return os.path.join(project_dir or repo_root(), "config")


def playlists_path(project_dir=None):
    return os.path.join(config_dir(project_dir), "playlists.json")


def master_tracks_path(project_dir=None):
    return os.path.join(config_dir(project_dir), "master_tracks_list.json")


def lobby_config_path(project_dir=None):
    return os.path.join(config_dir(project_dir), "lobby_config.json")


def load_lobby_config(project_dir=None):
    """Parse ``config/lobby_config.json``.

    Raises ``FileNotFoundError`` when absent. The orchestrator's ``load_config()``
    wraps this and turns that into its historical ``print`` + ``sys.exit(1)``; the
    dashboard degrades to defaults instead, which is why the exit lives at the call
    site and not in here.
    """
    path = lobby_config_path(project_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r") as f:
        return json.load(f)


def load_lobby_config_or_empty(project_dir=None):
    """``load_lobby_config`` that degrades to ``{}`` on any failure (dashboard side)."""
    try:
        return load_lobby_config(project_dir)
    except Exception:
        return {}


def resolve_liftoff_path(config):
    """Resolve the Liftoff executable path from lobby_config, auto-correcting a path
    that points at *another* user's home.

    Moved verbatim (behaviour included) out of ``run_headless_lobby.main()``: the repo's
    ``lobby_config.json`` hardcodes ``/home/fpv_bot/...`` but the same checkout is run by
    the dev user and inside Docker, so a ``/home/<other>`` prefix is rewritten to the
    current user's home when that alternative actually exists. Returns the path even if
    it does not exist (callers decide whether that is fatal).
    """
    liftoff_path = os.path.expanduser(config.get("liftoff_path", "") or "")
    if not liftoff_path or os.path.exists(liftoff_path):
        return liftoff_path

    import getpass

    parts = liftoff_path.split("/")
    if len(parts) > 2 and parts[1] == "home":
        parts[2] = getpass.getuser()
        alternative_path = "/".join(parts)
        if os.path.exists(alternative_path):
            return alternative_path
    return liftoff_path


def resolve_game_dir(config=None, project_dir=None):
    """Directory of the Liftoff install (parent of ``Liftoff.x86_64``).

    ``FPV_GAME_DIR`` overrides everything — the Docker image installs the game at
    ``/steam/Liftoff`` and the dashboard may run as a sidecar that never sees
    lobby_config.json's host-shaped path.
    """
    env_dir = os.environ.get("FPV_GAME_DIR")
    if env_dir:
        return env_dir
    if config is None:
        config = load_lobby_config_or_empty(project_dir)
    liftoff_path = resolve_liftoff_path(config)
    if not liftoff_path:
        return None
    return os.path.dirname(liftoff_path)


def resolve_plugins_dir(config=None, project_dir=None):
    """The BepInEx ``plugins/`` directory — the protocol-file rendezvous point.

    Precedence: ``FPV_PLUGINS_DIR`` env (explicit deployment override / tests), then
    ``<game_dir>/BepInEx/plugins``. Returns None when the game dir cannot be resolved
    at all, so callers can report "not configured" instead of writing into ``/BepInEx``.
    """
    env_dir = os.environ.get("FPV_PLUGINS_DIR")
    if env_dir:
        return env_dir
    game_dir = resolve_game_dir(config, project_dir)
    if not game_dir:
        return None
    return os.path.join(game_dir, "BepInEx", "plugins")


def bepinex_log_path(config=None, project_dir=None):
    """``<game_dir>/BepInEx/LogOutput.log`` (the plugin's own Unity-side log)."""
    game_dir = resolve_game_dir(config, project_dir)
    if not game_dir:
        return None
    return os.path.join(game_dir, "BepInEx", "LogOutput.log")


_PLAYER_LOG_SUFFIX = os.path.join(".config", "unity3d", "LuGus Studios", "Liftoff", "Player.log")


def player_log_candidates(config=None, project_dir=None):
    """Candidate paths for Unity's ``Player.log``, most specific first.

    There is no config key for it, so it is derived from the game dir two ways, which
    between them cover both deployments we actually run:

    - host/bare metal: ``/home/fpv_bot/.steam/debian-installation/.../Liftoff`` — the
      home directory is the prefix before ``/.steam/`` (or ``/.local/share/Steam/``);
    - Docker: game dir ``/steam/Liftoff`` with ``HOME=/steam`` — the home directory is
      the parent of the game dir.

    ``FPV_PLAYER_LOG`` overrides both.
    """
    override = os.environ.get("FPV_PLAYER_LOG")
    if override:
        return [override]

    game_dir = resolve_game_dir(config, project_dir)
    if not game_dir:
        return []

    homes = []
    for marker in ("/.steam/", "/.local/share/Steam/"):
        if marker in game_dir:
            homes.append(game_dir.split(marker)[0])
    homes.append(os.path.dirname(game_dir))

    seen = []
    for home in homes:
        candidate = os.path.join(home, _PLAYER_LOG_SUFFIX)
        if candidate not in seen:
            seen.append(candidate)
    return seen


def player_log_path(config=None, project_dir=None):
    """First existing ``Player.log`` candidate, else the first candidate, else None."""
    candidates = player_log_candidates(config, project_dir)
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0] if candidates else None
