"""CRUD over ``config/playlists.json``, validated with the existing ``trackcheck`` lib.

Editing playlists by hand is the thing this feature exists to replace, and the failure
mode it has to prevent is well documented: a typo'd track or environment name resolves to
zero tracks at runtime, and the resolver silently falls back to ``all_official_races``
(see ``trackcheck/lint_playlists.py`` — "instead of at 3am"). So a save runs the *same*
lint the pre-commit check runs, and classifies its findings:

- **blocking** — the entry is malformed or names something that is not a real Liftoff
  environment or game mode. These are always typos; saving one is never what was meant.
- **warning** — the entry is well-formed but matches nothing right now, resolves to an
  empty playlist, or repeats itself. Legitimate on a machine where that workshop track
  is not installed yet, so it is refused by default but overridable with ``force``.

Writes are atomic (temp + ``os.replace``): the orchestrator reads this file at startup
and on every playlist change, so a half-written JSON would be a genuine outage.
"""

import json
import os

from trackcheck.lint_playlists import lint_playlists

from . import paths as paths_mod

BLOCKING_CODES = {
    "INVALID_PLAYLIST_SHAPE",
    "INVALID_ENTRY_SHAPE",
    "UNKNOWN_ENVIRONMENT",
    "UNKNOWN_MODE",
}

# Deleting this one breaks the resolver's last-resort fallback (see
# playlists.resolve_and_write_playlist), which is what stands between a typo'd playlist
# and a bot stuck on an empty rotation.
PROTECTED_PLAYLISTS = {"all_official_races"}


class PlaylistStoreError(ValueError):
    """Base class for playlist-editing refusals; carries the lint findings."""

    def __init__(self, message, findings=None):
        super().__init__(message)
        self.findings = findings or []


class PlaylistValidationError(PlaylistStoreError):
    """The submitted playlist did not pass validation."""


def load_playlists(playlists_path=None, project_dir=None):
    path = playlists_path or paths_mod.playlists_path(project_dir)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise PlaylistStoreError(f"{path} does not contain a JSON object of playlists")
    return data


def save_playlists(data, playlists_path=None, project_dir=None):
    path = playlists_path or paths_mod.playlists_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "{}.tmp.{}".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def load_master(master_path=None, project_dir=None):
    """The generated track catalogue, or None when it does not exist.

    ``master_tracks_list.json`` is produced at runtime by ``gather_tracks.py`` from a live
    game install and is gitignored, so "absent" is a normal state on a fresh checkout —
    validation degrades to shape checks rather than refusing to work.
    """
    path = master_path or paths_mod.master_tracks_path(project_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def classify(findings):
    for finding in findings:
        finding["severity"] = "blocking" if finding["code"] in BLOCKING_CODES else "warning"
    return findings


def validate_playlist(name, items, master_data):
    """Lint one playlist definition. Returns findings (each with a ``severity``).

    Without a master list only the shape/vocabulary checks are meaningful, so
    match-count findings are dropped rather than reported against an empty catalogue
    (every entry would "match nothing", which is noise, not information).
    """
    if not isinstance(items, list):
        return classify([{"playlist": name, "code": "INVALID_PLAYLIST_SHAPE",
                          "detail": "playlist value must be a list of entries"}])

    findings = lint_playlists({name: items}, master_data if master_data is not None else {})
    if master_data is None:
        findings = [f for f in findings
                    if f["code"] not in ("ENTRY_NO_MATCHES", "EMPTY_RESOLUTION")]
    return classify(findings)


def blocking(findings):
    return [f for f in findings if f["severity"] == "blocking"]


def warnings(findings):
    return [f for f in findings if f["severity"] == "warning"]


def upsert_playlist(name, items, force=False, playlists_path=None, master_path=None,
                    project_dir=None):
    """Create/replace one playlist. Returns ``(playlists_data, findings)``."""
    if not name or not isinstance(name, str) or name.strip() != name or "/" in name:
        raise PlaylistStoreError(
            "Playlist name must be a non-empty string with no surrounding whitespace "
            "or '/' (it is written verbatim into playlist_name.txt).")

    master_data = load_master(master_path, project_dir)
    findings = validate_playlist(name, items, master_data)

    if blocking(findings):
        raise PlaylistValidationError(
            "Playlist '{}' has {} blocking problem(s).".format(name, len(blocking(findings))),
            findings)
    if warnings(findings) and not force:
        raise PlaylistValidationError(
            "Playlist '{}' has {} warning(s); re-submit with force=true to save "
            "anyway.".format(name, len(warnings(findings))), findings)

    data = load_playlists(playlists_path, project_dir)
    data[name] = items
    save_playlists(data, playlists_path, project_dir)
    return data, findings


def delete_playlist(name, active_playlist=None, playlists_path=None, project_dir=None):
    data = load_playlists(playlists_path, project_dir)
    if name not in data:
        raise PlaylistStoreError(f"No such playlist: {name}")
    if name in PROTECTED_PLAYLISTS:
        raise PlaylistStoreError(
            f"'{name}' is the resolver's fallback playlist and cannot be deleted.")
    if active_playlist and name == active_playlist:
        raise PlaylistStoreError(
            f"'{name}' is the active playlist; switch to another one before deleting it.")
    del data[name]
    save_playlists(data, playlists_path, project_dir)
    return data


def lint_all(playlists_path=None, master_path=None, project_dir=None):
    """Lint every playlist, for the manager view's per-playlist status badges."""
    data = load_playlists(playlists_path, project_dir)
    master_data = load_master(master_path, project_dir)
    by_playlist = {}
    for name, items in data.items():
        by_playlist[name] = validate_playlist(name, items, master_data)
    return data, by_playlist, master_data is not None
