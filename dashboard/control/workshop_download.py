"""In-game workshop download, orchestrator side.

See ``docs/features/doing/workshop-ingame-download.md``. The plugin half
(``plugin/WorkshopDownloader.cs``) can pull any published file id into the *running*
game through Steamworks; this half drives it and decides whether the result is allowed
anywhere near the rotation:

    request_workshop_download()      write workshop_download_request.txt (one id)
      -> poll for the result file    the plugin's DownloadItemResult_t outcome
      -> trackcheck.validate_item()  is this actually a usable, safe track?
         -> quarantine on failure    moved aside + a `quarantine` JSONL event
      -> gather_tracks_and_races()   the existing track-database refresh
      -> resolve_and_write_playlist() the existing resolver; rotation picks it up live

Three properties this module exists to guarantee:

- **The callback is the success signal, not the absence of an error.** The plugin only
  writes ``ok`` when Steam's ``DownloadItemResult_t`` said ``k_EResultOK``; this side
  adds nothing to that and never infers success from a file appearing on disk.
- **Nothing unvalidated reaches the rotation.** Validation happens *before* the
  re-gather, because ``gather_tracks_and_races()`` is what makes a track resolvable --
  once it has run, the track is a rotation candidate. A workshop id can point at a
  corrupt or hostile item regardless of which route fetched it; the in-game path is not
  more trusted than the steamcmd one.
- **One gather/resolve path.** Both steps call the same functions the orchestrator's own
  startup and the first-run bootstrap call (AGENTS.md rule 4) -- they are injectable here
  only so the state machine is unit-testable without a game or a Steam client.

Blocking by design (a bounded ~130s wait, just past the plugin's own 120s budget), so it
is invoked from a CLI/dashboard action, never from the orchestrator's 1-second monitor
loop.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import paths  # noqa: F401  (import performs the orchestrator sys.path bootstrap)

from workshop_items import quarantine_item, workshop_content_root, workshop_item_dir  # noqa: E402

# Just past the plugin's own 120s DownloadItemResult_t timeout, so a plugin that is alive
# always gets to answer first (with a specific reason) and this bound only ever trips when
# the plugin is not running / not reading its protocol directory at all.
DEFAULT_TIMEOUT_SECONDS = 130.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# `kind` for the `decision` events this module emits, and the quarantine sub-directory /
# event `source` for items it rejects (workshop-steamcmd-install.md will use its own).
DECISION_KIND = "workshop_download"
QUARANTINE_SOURCE = "ingame_download"

# Failure reasons this side can produce. The plugin's own vocabulary (bad_id,
# download_rejected, <EResult name>, timeout) passes through untouched; these are the
# ones only the orchestrator can observe.
REASON_WATCHER_TIMEOUT = "watcher_timeout"
REASON_ITEM_DIR_MISSING = "item_dir_missing"
REASON_VALIDATION_FAILED = "validation_failed"
REASON_GATHER_FAILED = "gather_failed"


@dataclass
class DownloadOutcome:
    """What happened, in one object. ``ok`` is the only thing a caller must branch on."""

    published_id: str
    ok: bool
    reason: str = ""
    detail: Optional[str] = None
    item_dir: Optional[str] = None
    quarantine_path: Optional[str] = None
    validation_reasons: List[str] = field(default_factory=list)
    resolved_count: Optional[int] = None

    def summary(self):
        if self.ok:
            extra = " ({})".format(self.reason) if self.reason else ""
            if self.resolved_count is not None:
                extra += "; playlist now resolves to {} tracks".format(self.resolved_count)
            return "workshop item {} installed and validated{}".format(self.published_id, extra)
        detail = ": {}".format(self.detail) if self.detail else ""
        return "workshop item {} failed ({}){}".format(self.published_id, self.reason, detail)


def _default_gather():
    # The same entry point run_headless_lobby.py's startup and control/bootstrap.py use.
    from gather_tracks import gather_tracks_and_races
    return gather_tracks_and_races()


def _default_resolve(playlist_name, shuffle, tracks_file, logger=None):
    from .playlists import resolve_and_write_playlist
    return resolve_and_write_playlist(playlist_name, shuffle, tracks_file, logger=logger)


def _default_validate(item_dir, race_search_dirs=None):
    from trackcheck.validate import validate_item
    return validate_item(item_dir, race_search_dirs=race_search_dirs)


def _race_search_dirs(item_dir, game_dir=None):
    """Where to look for the ``.race`` that matches the downloaded ``.track``.

    The item's own directory first (the normal case: a workshop item ships its track and
    race together), plus the whole workshop content root, because a race can legitimately
    live in a *different* item that was published separately -- and rejecting a perfectly
    good track for that would quarantine it for someone else's packaging choice. The
    extra recursive glob costs nothing here: this runs once per download, not per tick.
    """
    dirs = [item_dir]
    root = workshop_content_root(game_dir=game_dir)
    if root and root not in dirs:
        dirs.append(root)
    return dirs


def wait_for_result(protocol, published_id, timeout=DEFAULT_TIMEOUT_SECONDS,
                    poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                    clock=time.monotonic, sleep=time.sleep, logger=None):
    """Block until the plugin publishes a result for ``published_id``, or the bound trips.

    Returns the parsed record (``{"published_id", "ok", "reason"}``) or None on timeout.
    A result for a *different* id is consumed and ignored: it belongs to an ``/dl`` a chat
    admin ran, and leaving it in place would make it outlive this wait and be mistaken for
    the answer to the next request.
    """
    deadline = clock() + timeout
    while True:
        record = protocol.consume_workshop_download_result()
        if record is not None:
            if record["published_id"] == str(published_id).strip():
                return record
            message = ("discarding a workshop download result for {} while waiting for {} "
                       "(an /dl command finishing in parallel)".format(
                           record["published_id"], published_id))
            print("[Workshop] {}".format(message))
            if logger is not None:
                logger.decision(DECISION_KIND, message)
        if clock() >= deadline:
            return None
        sleep(poll_interval)


def download_workshop_item(published_id, protocol, game_dir=None, playlist_name=None,
                           tracks_file=None, shuffle=False, logger=None,
                           timeout=DEFAULT_TIMEOUT_SECONDS,
                           poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                           clock=time.monotonic, sleep=time.sleep,
                           project_dir=None, gather=None, resolve=None, validate=None,
                           quarantine=None, resolve_item_dir=None):
    """Download one workshop item into the running game and make it rotatable.

    ``protocol`` is a ``ProtocolDir`` for the plugin's BepInEx ``plugins/`` directory.
    ``playlist_name``/``tracks_file`` are optional: without them the track database is
    still refreshed (so the item is resolvable from then on) but no rotation file is
    rewritten -- which is the right behaviour for an install running a hand-edited
    ``tracks_to_rotate.txt`` rather than a named playlist.
    """
    published_id = str(published_id).strip()
    if not published_id:
        # Same reason code the plugin uses for input it cannot parse -- an empty id is
        # that same condition, just caught before a meaningless request file is written
        # (see ProtocolDir.request_workshop_download). Not a second validator: every
        # non-empty id still goes to the plugin unexamined.
        return _fail(published_id, "bad_id", logger, detail="no workshop id given")
    gather = gather or _default_gather
    resolve = resolve or _default_resolve
    validate = validate or _default_validate
    quarantine = quarantine or quarantine_item
    resolve_item_dir = resolve_item_dir or workshop_item_dir

    # Any leftover result (a previous run that died, or an /dl that finished while nobody
    # was listening) must go before the request, or it would be read as this one's answer.
    stale = protocol.consume_workshop_download_result()
    if stale is not None:
        print("[Workshop] Discarded a stale download result for {} before requesting {}.".format(
            stale["published_id"], published_id))

    print("[Workshop] Requesting in-game download of workshop item {}...".format(published_id))
    protocol.request_workshop_download(published_id)
    if logger is not None:
        logger.decision(DECISION_KIND,
                        "requested in-game download of workshop item {} "
                        "(waiting up to {:.0f}s for the plugin's result)".format(
                            published_id, timeout))

    record = wait_for_result(protocol, published_id, timeout=timeout,
                             poll_interval=poll_interval, clock=clock, sleep=sleep,
                             logger=logger)
    if record is None:
        return _fail(published_id, REASON_WATCHER_TIMEOUT, logger,
                     detail=("no workshop_download_result.txt after {:.0f}s -- is the plugin "
                             "running and reading {}?".format(timeout, protocol.plugins_dir)))
    if not record["ok"]:
        # The plugin's own reason (bad_id / download_rejected / <EResult> / timeout) is the
        # whole message; this side has nothing to add to it.
        return _fail(published_id, record["reason"] or "unknown", logger)

    item_dir = resolve_item_dir(published_id, game_dir=game_dir)
    if not item_dir:
        return _fail(published_id, REASON_ITEM_DIR_MISSING, logger,
                     detail=("Steam reported the download OK but no directory for {} exists "
                             "under the workshop content root".format(published_id)))

    report = validate(item_dir, race_search_dirs=_race_search_dirs(item_dir, game_dir=game_dir))
    if not report.ok:
        # trackcheck reasons are a str-Enum: `.value` is the bare "GATE_DATA_MISSING"
        # code, while str() would render "Reason.GATE_DATA_MISSING". The bare code is
        # what the quarantine manifest, the JSONL event and any grep want.
        reasons = [getattr(r, "value", str(r)) for r in report.reasons]
        quarantine_path = None
        try:
            manifest = quarantine(item_dir, reasons, QUARANTINE_SOURCE,
                                  project_dir=project_dir, logger=logger,
                                  published_id=published_id)
            quarantine_path = manifest.get("quarantine_path")
        except Exception as exc:  # quarantine failing must not mask the rejection
            print("[Workshop] ERROR: failed to quarantine {}: {}".format(item_dir, exc))
            if logger is not None:
                logger.error("failed to quarantine rejected workshop item {}: {}".format(
                    published_id, exc), context=DECISION_KIND)
        outcome = _fail(published_id, REASON_VALIDATION_FAILED, logger,
                        detail=", ".join(reasons))
        outcome.item_dir = item_dir
        outcome.validation_reasons = reasons
        outcome.quarantine_path = quarantine_path
        return outcome

    # Only now -- validated -- does the item become a rotation candidate.
    try:
        gather()
        resolved_count = None
        if playlist_name and tracks_file:
            resolved = resolve(playlist_name, shuffle, tracks_file, logger=logger)
            resolved_count = len(resolved or [])
    except Exception as exc:
        return _fail(published_id, REASON_GATHER_FAILED, logger, detail=str(exc))

    outcome = DownloadOutcome(published_id=published_id, ok=True, reason=record["reason"],
                              item_dir=item_dir, resolved_count=resolved_count)
    print("[Workshop] {}".format(outcome.summary()))
    if logger is not None:
        logger.decision(DECISION_KIND, outcome.summary())
    return outcome


def _fail(published_id, reason, logger, detail=None):
    outcome = DownloadOutcome(published_id=published_id, ok=False, reason=reason, detail=detail)
    print("[Workshop] ERROR: {}".format(outcome.summary()))
    if logger is not None:
        logger.error(outcome.summary(), context=DECISION_KIND)
    return outcome
