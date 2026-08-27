"""Raw log file discovery + tailing for the dashboard's log browser.

Three log families exist on a bot host, and an operator debugging a session needs all
three (this is the "show log files" operator ask):

- the shared structured JSONL (``<log_dir>/bot-YYYY-MM-DD.jsonl``) — what the dashboard
  streams anyway, but sometimes you want the raw bytes;
- BepInEx's ``LogOutput.log`` — the plugin's own Unity-side log, where anything that did
  not make it into a structured event lands;
- Unity's ``Player.log`` — the game's log, where crashes and Steam/Photon failures land.

Every path is *discovered*, never accepted from the client: the API addresses logs by an
opaque id taken from this catalogue, so no request can name an arbitrary file. Files that
are configured but missing or unreadable are still listed, with the reason — on a
bare-metal host the last two are owned by ``fpv_bot`` and a dashboard running as another
user legitimately cannot read them, and silently hiding them would look like a bug.
"""

import os

from . import events as events_mod
from . import paths as paths_mod


def _stat_entry(entry):
    path = entry["path"]
    entry["exists"] = os.path.exists(path)
    entry["readable"] = os.access(path, os.R_OK) if entry["exists"] else False
    entry["size"] = None
    entry["mtime"] = None
    if entry["exists"]:
        try:
            st = os.stat(path)
            entry["size"] = st.st_size
            entry["mtime"] = st.st_mtime
        except OSError:
            entry["readable"] = False
    if not entry["exists"]:
        entry["note"] = "not present"
    elif not entry["readable"]:
        entry["note"] = "not readable by the dashboard process (check file ownership)"
    else:
        entry["note"] = None
    return entry


def list_logs(config=None, project_dir=None, log_dir=None):
    """The log catalogue: JSONL dailies (newest first) plus the two Unity-side logs."""
    if config is None:
        config = paths_mod.load_lobby_config_or_empty(project_dir)
    if log_dir is None:
        log_dir = paths_mod.resolve_log_dir(config, project_dir)

    catalogue = []
    if log_dir:
        for daily in events_mod.list_daily_files(log_dir):
            catalogue.append(_stat_entry({
                "id": "jsonl:" + daily["name"],
                "kind": "jsonl",
                "label": daily["name"],
                "path": daily["path"],
            }))

    bepinex = paths_mod.bepinex_log_path(config, project_dir)
    if bepinex:
        catalogue.append(_stat_entry({
            "id": "bepinex",
            "kind": "plugin",
            "label": "BepInEx/LogOutput.log",
            "path": bepinex,
        }))

    player = paths_mod.player_log_path(config, project_dir)
    if player:
        catalogue.append(_stat_entry({
            "id": "player",
            "kind": "game",
            "label": "Unity Player.log",
            "path": player,
        }))

    return catalogue


def find_log(log_id, config=None, project_dir=None, log_dir=None):
    """Look an id up in the catalogue. Returns None for anything not in it — which is
    what makes ``../../etc/passwd`` unreachable: ids are matched, never joined."""
    for entry in list_logs(config, project_dir, log_dir):
        if entry["id"] == log_id:
            return entry
    return None


def tail_file(path, lines=200, max_bytes=1024 * 1024):
    """Last ``lines`` lines of a file, reading at most ``max_bytes`` from the end.

    Player.log routinely reaches tens of MB (the room-churn investigation waded through
    ~83,000 lines), so the whole file is never read into memory.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            data = f.read()
    except OSError as e:
        raise LogUnreadableError(str(e))
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


class LogUnreadableError(RuntimeError):
    """The file exists in the catalogue but could not be read (permissions, races)."""
