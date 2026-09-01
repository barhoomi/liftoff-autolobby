"""Workshop content on this machine: where items land, and how a bad one is quarantined.

Two install-side concerns that every workshop path needs and that nothing else owns:

1. **Where Steam puts a workshop item.** The layout is
   ``<steam>/steamapps/workshop/content/410340/<published_file_id>/`` -- confirmed by
   decompile (``SteamUGC.CreateItem((AppId_t)410340u, ...)`` plus ``GetItemInstallInfo``'s
   ``pchFolder``, see ``docs/features/done/workshop-ingame-download-spike.md`` Q5) and by
   the fact ``gather_tracks.py`` has scanned exactly this shape since long before either
   workshop feature existed. That candidate list used to be inlined in
   ``gather_tracks.py``; it lives here now so the gatherer and the in-game downloader
   cannot drift apart about where a track is (AGENTS.md rule 4).

2. **Quarantine.** A workshop id can point at a corrupt -- or hostile -- item no matter
   how it was fetched, so an item that fails ``trackcheck`` must be moved out of the way
   *before* anything re-gathers the track database, or the next
   ``resolve_and_write_playlist`` can hand the plugin a track the game will choke on.
   Quarantining is deliberately "move aside + record why", never delete: the operator
   needs the files to diagnose a rejection, and a delete inside a Steam content directory
   is the kind of irreversible thing this project does not do to itself.

Shared on purpose: ``docs/features/todo/workshop-steamcmd-install.md`` (C2) installs the
same kind of item by a different route and must reuse this quarantine, not invent a
second one with a second event shape.
"""

import getpass
import json
import os
import shutil
from datetime import datetime, timezone

# Liftoff's Steam AppID. Decompile-confirmed (spike Q3/Q5), also the value the
# orchestrator exports as SteamAppId when it launches the game.
LIFTOFF_APP_ID = 410340

WORKSHOP_CONTENT_ENV_VAR = "FPV_WORKSHOP_CONTENT_DIR"
QUARANTINE_ENV_VAR = "FPV_QUARANTINE_DIR"

DEFAULT_QUARANTINE_DIRNAME = "quarantine"

# Name of the sidecar written next to every quarantined item, so the reason survives
# independently of the log file (logs rotate daily and are volume-mounted; a directory
# that turns up in six months should explain itself).
QUARANTINE_MANIFEST = "quarantine.json"


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def workshop_content_roots(game_dir=None, env=None):
    """Candidate ``.../steamapps/workshop/content/410340`` directories, best first.

    ``FPV_WORKSHOP_CONTENT_DIR`` wins outright (the container's install lives wherever
    ``force_install_dir`` put it). Then the three historical per-user Steam layouts, in
    the order ``gather_tracks.py`` has always tried them -- unchanged, so this refactor
    cannot alter which directory an existing host resolves to. The ``game_dir``-derived
    candidate is appended last for exactly that reason: it is strictly additional
    coverage (a Liftoff install under ``steamapps/common/Liftoff`` implies a sibling
    ``steamapps/workshop``), never a reordering of what already worked.
    """
    env = os.environ if env is None else env
    roots = []

    override = env.get(WORKSHOP_CONTENT_ENV_VAR)
    if override:
        roots.append(override)

    user = getpass.getuser()
    roots.extend([
        "/home/{}/.steam/debian-installation/steamapps/workshop/content/{}".format(user, LIFTOFF_APP_ID),
        "/home/{}/.steam/steam/steamapps/workshop/content/{}".format(user, LIFTOFF_APP_ID),
        "/home/{}/.local/share/Steam/steamapps/workshop/content/{}".format(user, LIFTOFF_APP_ID),
    ])

    if game_dir:
        # <steamapps>/common/Liftoff -> <steamapps>/workshop/content/410340
        steamapps = os.path.dirname(os.path.dirname(os.path.abspath(game_dir)))
        roots.append(os.path.join(steamapps, "workshop", "content", str(LIFTOFF_APP_ID)))

    return roots


def workshop_content_root(game_dir=None, env=None):
    """The first candidate root that exists on disk, or None if none do."""
    for path in workshop_content_roots(game_dir=game_dir, env=env):
        if os.path.isdir(path):
            return path
    return None


def workshop_item_dir(published_id, game_dir=None, env=None):
    """Absolute directory of one downloaded workshop item, or None.

    Returns None both when no content root exists and when the item's own directory is
    absent -- the caller's question is always "are the files there", and "Steam said ok
    but the folder isn't there" is a failure, not a path to keep working with.
    """
    root = workshop_content_root(game_dir=game_dir, env=env)
    if not root:
        return None
    candidate = os.path.join(root, str(published_id).strip())
    return candidate if os.path.isdir(candidate) else None


def quarantine_root(project_dir=None, env=None):
    """Where quarantined items are moved to (``FPV_QUARANTINE_DIR`` or ``<repo>/quarantine``)."""
    env = os.environ if env is None else env
    override = env.get(QUARANTINE_ENV_VAR)
    if override:
        return override
    return os.path.join(project_dir or _repo_root(), DEFAULT_QUARANTINE_DIRNAME)


def quarantine_item(item_dir, reasons, source, project_dir=None, env=None, logger=None,
                    now=None, published_id=None):
    """Move a rejected item out of the content tree and record why.

    Returns the manifest dict (also written as ``quarantine.json`` beside the moved
    files, and emitted as one ``quarantine`` JSONL event when a ``logger`` is given).

    ``shutil.move``, not ``os.rename``: the content root lives under the Steam install
    and the quarantine directory normally does not, so this is routinely a cross-device
    move that ``os.rename`` would fail with EXDEV.

    Raises ``FileNotFoundError`` if ``item_dir`` does not exist -- a caller asking to
    quarantine something that isn't there has a bug, and silently succeeding would let
    an unvalidated item stay in the rotation's way.
    """
    if not item_dir or not os.path.isdir(item_dir):
        raise FileNotFoundError("cannot quarantine '{}': not a directory".format(item_dir))

    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    root = quarantine_root(project_dir=project_dir, env=env)
    dest_parent = os.path.join(root, source)
    os.makedirs(dest_parent, exist_ok=True)

    basename = os.path.basename(os.path.normpath(item_dir))
    dest = os.path.join(dest_parent, "{}-{}".format(basename, stamp))
    shutil.move(item_dir, dest)

    manifest = {
        "quarantined_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "published_id": str(published_id) if published_id is not None else basename,
        "original_path": os.path.abspath(item_dir),
        "quarantine_path": os.path.abspath(dest),
        "reasons": [getattr(r, "value", str(r)) for r in (reasons or [])],
    }
    try:
        with open(os.path.join(dest, QUARANTINE_MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
    except OSError as exc:  # the move already happened; a missing sidecar must not undo it
        print("[Quarantine] WARNING: failed to write {}: {}".format(QUARANTINE_MANIFEST, exc))

    print("[Quarantine] Moved {} -> {} ({})".format(
        manifest["original_path"], manifest["quarantine_path"],
        ", ".join(manifest["reasons"]) or "no reason given"))

    if logger is not None:
        logger.emit("quarantine",
                    item=manifest["published_id"],
                    source=source,
                    reasons=manifest["reasons"],
                    original_path=manifest["original_path"],
                    quarantine_path=manifest["quarantine_path"])
    return manifest
