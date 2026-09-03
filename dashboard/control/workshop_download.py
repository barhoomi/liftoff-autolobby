"""In-game workshop download, orchestrator side — the BLOCKING entry point.

See ``docs/features/doing/workshop-ingame-download.md`` and
``docs/features/doing/workshop-ingest-hardening.md``. The plugin half
(``plugin/WorkshopDownloader.cs``) can pull any published file id into the *running*
game through Steamworks; this half drives it and decides whether the result is allowed
anywhere near the rotation:

    request_workshop_download()      write workshop_download_request.txt (one id)
      -> poll for the result file    the plugin's DownloadItemResult_t outcome
      -> (repeat, id by id)          a track and its race are separate workshop items
      -> validate_and_quarantine_batch()  is this actually a usable, safe SET?
      -> wait_for_fresh_dump()       the plugin's re-run availability sweep
      -> finalize_ingest()           gather, "does the game list it?", resolve

Everything after the download is in ``workshop_ingest.py`` and is shared verbatim with
the orchestrator's non-blocking auto-ingest (AGENTS.md rule 4). What is left here is the
one thing that genuinely differs: **this side blocks**, because a human is watching its
exit code. That blocking wait must never enter the monitor loop — see ``WorkshopIngest``.

Three properties this path exists to guarantee:

- **The callback is the success signal, not the absence of an error.** The plugin only
  writes ``ok`` when Steam's ``DownloadItemResult_t`` said ``k_EResultOK``; this side
  adds nothing to that and never infers success from a file appearing on disk.
- **Nothing unvalidated reaches the rotation.** Validation happens *before* the
  re-gather, because ``gather_tracks_and_races()`` is what makes a track resolvable --
  once it has run, the track is a rotation candidate. A workshop id can point at a
  corrupt or hostile item regardless of which route fetched it; the in-game path is not
  more trusted than the steamcmd one.
- **One gather/resolve path.** Both steps call the same functions the orchestrator's own
  startup and the first-run bootstrap call -- they are injectable here only so the state
  machine is unit-testable without a game or a Steam client.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import paths  # noqa: F401  (import performs the orchestrator sys.path bootstrap)
from .workshop_ingest import (  # noqa: F401  (several are re-exported on purpose)
    BatchOutcome,
    DECISION_KIND,
    QUARANTINE_SOURCE,
    REASON_BAD_ID,
    REASON_GAME_LISTING_MISSING,
    REASON_GATHER_FAILED,
    REASON_ITEM_DIR_MISSING,
    REASON_SWEEP_TIMEOUT,
    REASON_VALIDATION_FAILED,
    REASON_WATCHER_TIMEOUT,
    REASON_RESOLVE_FAILED,
    availability_dump_mtime,
    fail_batch,
    finalize_ingest,
    race_search_dirs,
    sweep_blocked_detail,
    sweep_failure,
    sweep_satisfied,
    sweep_timeout_seconds,
    tracks_to_confirm,
    validate_and_quarantine_batch,
)

from workshop_items import workshop_item_dir  # noqa: E402

# Just past the plugin's own 120s DownloadItemResult_t timeout, so a plugin that is alive
# always gets to answer first (with a specific reason) and this bound only ever trips when
# the plugin is not running / not reading its protocol directory at all.
DEFAULT_TIMEOUT_SECONDS = 130.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# How often the sweep wait tells the operator it is still waiting. The wait itself is
# minutes long by design (the sweep only runs when the next rotation opens the settings
# popup), and silence for that long reads as a hang.
SWEEP_PROGRESS_INTERVAL_SECONDS = 30.0


@dataclass
class DownloadOutcome:
    """What happened to ONE id, in one object. ``ok`` is the only thing to branch on.

    Kept exactly as it was: ``orchestrator/download_workshop_item.py`` and this module's
    tests are written against it, and the multi-id flow projects its batch onto it.
    """

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


def wait_for_result(protocol, published_id, timeout=DEFAULT_TIMEOUT_SECONDS,
                    poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                    clock=time.monotonic, sleep=time.sleep, logger=None, on_poll=None,
                    own_ids=None):
    """Block until the plugin publishes a result for ``published_id``, or the bound trips.

    Returns the parsed record (``{"published_id", "ok", "reason"}``) or None on timeout.

    A result for an id this run does **not** own is LEFT WHERE IT IS. It belongs to a chat
    ``/dl``, and the auto-ingest is what will finish it — consuming and discarding it (as
    this function used to) meant a download the plugin had already announced in chat as
    "ingesting now" was never validated, never gathered and never resolved by anybody. The
    claim file makes the arbitration symmetric: the auto-ingest leaves this run's results
    alone, and this run leaves the auto-ingest's alone.

    ``own_ids`` is the whole batch this run is driving; a result for one of those *other*
    ids is a duplicate of an outcome already handled, so it is consumed and dropped.

    ``on_poll`` runs once per iteration; the batch flow uses it to refresh its claim file's
    mtime, so a legitimately long run never looks abandoned to the auto-ingest.
    """
    wanted = str(published_id).strip()
    mine = set(str(i).strip() for i in (own_ids or ())) | {wanted}
    announced = set()
    deadline = clock() + timeout
    while True:
        record = protocol.consume_workshop_download_result(
            accept=lambda r: r["published_id"] in mine)
        if record is not None and record["published_id"] == wanted:
            return record
        if record is not None:
            print("[Workshop] Discarding a duplicate result for {} while waiting for "
                  "{}.".format(record["published_id"], wanted))
        else:
            foreign = protocol.read_workshop_download_result()
            if foreign is not None and foreign["published_id"] not in announced:
                announced.add(foreign["published_id"])
                message = ("leaving the workshop download result for {} in place while "
                           "waiting for {}: it belongs to a chat /dl, and the "
                           "orchestrator's auto-ingest is what finishes it".format(
                               foreign["published_id"], wanted))
                print("[Workshop] {}".format(message))
                if logger is not None:
                    logger.decision(DECISION_KIND, message)
        if clock() >= deadline:
            return None
        if on_poll is not None:
            on_poll()
        sleep(poll_interval)


def wait_for_fresh_dump(plugins_dir, baseline_mtime, tracks, timeout,
                        poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                        clock=time.monotonic, sleep=time.sleep, logger=None, on_poll=None):
    """Block until the plugin's availability sweep has re-run, or the bound trips.

    Minutes long by design and that is not a bug: the sweep only happens when the next
    rotation re-opens the settings popup, and it advances one dropdown step per 1s tick.
    ``--skip-now`` is the opt-in accelerator.
    """
    deadline = clock() + timeout
    last_progress = clock()
    started = clock()
    while True:
        if sweep_satisfied(plugins_dir, baseline_mtime, tracks):
            return True
        now = clock()
        if now >= deadline:
            return False
        if (now - last_progress) >= SWEEP_PROGRESS_INTERVAL_SECONDS:
            last_progress = now
            print("[Workshop] waiting for the plugin's availability re-sweep "
                  "({:.0f}s elapsed of {:.0f}s)...".format(now - started, timeout))
        if on_poll is not None:
            on_poll()
        sleep(poll_interval)


def download_workshop_items(published_ids, protocol, game_dir=None, playlist_name=None,
                            tracks_file=None, shuffle=False, logger=None,
                            timeout=DEFAULT_TIMEOUT_SECONDS,
                            poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                            clock=time.monotonic, sleep=time.sleep,
                            project_dir=None, gather=None, resolve=None, validate=None,
                            quarantine=None, resolve_item_dir=None, sweep_timeout=None,
                            skip_now=False):
    """Download a SET of workshop items into the running game and make them rotatable.

    A set, not one id, because Liftoff publishes a track and its race as separate workshop
    items: ingesting either one alone deadlocks (see ``validate_item_set``). The ids go to
    the plugin **one at a time** through the existing single-id request/result files — the
    protocol is unchanged; only the caller batches.

    On a per-id *download* failure the batch stops there and nothing is quarantined:
    half-set validation verdicts are meaningless (the missing member is exactly what the
    remaining one would be judged against).
    """
    resolve_item_dir = resolve_item_dir or workshop_item_dir

    ids = []
    for raw in (published_ids or []):
        value = str(raw).strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        # Same reason code the plugin uses for input it cannot parse -- an empty id is
        # that same condition, just caught before a meaningless request file is written.
        return fail_batch([], REASON_BAD_ID, logger, detail="no workshop id given")

    # Captured BEFORE the first request: the sweep we are waiting for must be newer than
    # the ingest itself, and a baseline taken afterwards could accidentally be satisfied
    # by the sweep that our own download triggered... or miss one entirely.
    baseline_mtime = availability_dump_mtime(protocol.plugins_dir)

    protocol.claim_workshop_downloads(ids)
    try:
        return _run_batch(ids, protocol, baseline_mtime, game_dir=game_dir,
                          playlist_name=playlist_name, tracks_file=tracks_file,
                          shuffle=shuffle, logger=logger, timeout=timeout,
                          poll_interval=poll_interval, clock=clock, sleep=sleep,
                          project_dir=project_dir, gather=gather, resolve=resolve,
                          validate=validate, quarantine=quarantine,
                          resolve_item_dir=resolve_item_dir, sweep_timeout=sweep_timeout,
                          skip_now=skip_now)
    finally:
        # Always: an unreleased claim makes the auto-ingest ignore results until the
        # staleness bound expires.
        protocol.release_workshop_downloads()


def _run_batch(ids, protocol, baseline_mtime, game_dir, playlist_name, tracks_file,
               shuffle, logger, timeout, poll_interval, clock, sleep, project_dir,
               gather, resolve, validate, quarantine, resolve_item_dir, sweep_timeout,
               skip_now):
    def refresh_claim():
        protocol.claim_workshop_downloads(ids)

    # A leftover result for one of OUR ids (a previous run that died mid-wait) must go
    # before the request, or it would be read as this one's answer. A leftover for anyone
    # else's id is deliberately left alone: it is a chat /dl's outcome, and eating it here
    # -- as this used to -- means that download is never ingested by anybody. Nothing below
    # can mistake it for ours, because wait_for_result now only accepts our own ids.
    stale = protocol.consume_workshop_download_result(
        accept=lambda r: r["published_id"] in set(ids))
    if stale is not None:
        print("[Workshop] Discarded a stale download result for {} before requesting {}.".format(
            stale["published_id"], ", ".join(ids)))

    plugin_reasons = {}
    for published_id in ids:
        print("[Workshop] Requesting in-game download of workshop item {}...".format(
            published_id))
        protocol.request_workshop_download(published_id)
        if logger is not None:
            logger.decision(DECISION_KIND,
                            "requested in-game download of workshop item {} "
                            "(waiting up to {:.0f}s for the plugin's result)".format(
                                published_id, timeout))

        record = wait_for_result(protocol, published_id, timeout=timeout,
                                 poll_interval=poll_interval, clock=clock, sleep=sleep,
                                 logger=logger, on_poll=refresh_claim, own_ids=ids)
        if record is None:
            return fail_batch(
                ids, REASON_WATCHER_TIMEOUT, logger,
                detail=("no workshop_download_result.txt for {} after {:.0f}s. Is the "
                        "plugin running and reading {}? If an admin's /dl batch is "
                        "downloading, the plugin services this request only once that "
                        "queue drains -- the request file is still there, and the "
                        "orchestrator's auto-ingest will finish the ingest without "
                        "this command.".format(published_id, timeout,
                                               protocol.plugins_dir)),
                plugin_reasons=plugin_reasons)
        if not record["ok"]:
            # The plugin's own reason (bad_id / download_rejected / queue_full / <EResult>
            # / timeout) is the whole message; this side has nothing to add to it. Stop
            # the batch here: nothing has been validated, so nothing is quarantined.
            return fail_batch(ids, record["reason"] or "unknown", logger,
                              detail="workshop item {} failed in the plugin".format(
                                  published_id),
                              plugin_reasons=plugin_reasons)
        plugin_reasons[published_id] = record["reason"]

    # AFTER the downloads and BEFORE the sweep wait (§2.4). Firing it before the batch --
    # as this used to -- rotates while the items are still downloading, so the sweep it
    # triggers runs too early to see them and the flag ends up DELAYING the re-sweep it
    # exists to accelerate (review finding 10, 2026-09-04). skip_now.txt is consumed from
    # HandleGameRoom, i.e. in the waiting room, so it never interrupts a running race.
    if skip_now:
        protocol.trigger_skip_now()
        print("[Workshop] Requested an immediate rotation, so the re-sweep happens at the "
              "end of the current race instead of at the rotation timer.")

    item_dirs = {}
    for published_id in ids:
        item_dir = resolve_item_dir(published_id, game_dir=game_dir)
        if not item_dir:
            return fail_batch(
                ids, REASON_ITEM_DIR_MISSING, logger,
                detail=("Steam reported the download OK but no directory for {} exists "
                        "under the workshop content root".format(published_id)),
                item_dirs=item_dirs, plugin_reasons=plugin_reasons)
        item_dirs[published_id] = item_dir

    surviving, reports, extras = validate_and_quarantine_batch(
        item_dirs, race_search_dirs=race_search_dirs(list(item_dirs.values()),
                                                     game_dir=game_dir),
        validate=validate, quarantine=quarantine, protocol=protocol,
        project_dir=project_dir, logger=logger)

    if len(surviving) != len(item_dirs):
        # Any rejected member fails the batch: unvalidated content must never reach the
        # rotation, and a half-ingested pair is not a thing worth having.
        reasons = sorted({r for rs in extras["rejected_reasons"].values() for r in rs})
        return fail_batch(ids, REASON_VALIDATION_FAILED, logger,
                          detail=", ".join(reasons), item_dirs=item_dirs,
                          quarantined=dict(extras["quarantined"]),
                          validation_reasons=dict(extras["rejected_reasons"]),
                          warnings=dict(extras["warnings"]),
                          plugin_reasons=plugin_reasons)

    tracks = tracks_to_confirm([reports[i] for i in surviving])
    bound = sweep_timeout_seconds(protocol, override=sweep_timeout)
    if not wait_for_fresh_dump(protocol.plugins_dir, baseline_mtime, tracks, bound,
                               poll_interval=poll_interval, clock=clock, sleep=sleep,
                               logger=logger, on_poll=refresh_claim):
        # Two different things to tell an operator, and the baseline is what tells them
        # apart: a sweep that ran without listing our track (the files are fine and are NOT
        # quarantined) versus no sweep at all (usually operator state, not a fault).
        reason, detail = sweep_failure(protocol.plugins_dir, baseline_mtime, tracks,
                                       protocol, elapsed=bound)
        return fail_batch(ids, reason, logger, detail=detail, item_dirs=item_dirs,
                          warnings=dict(extras["warnings"]),
                          plugin_reasons=plugin_reasons)

    outcome = finalize_ingest(surviving, reports, protocol.plugins_dir, gather=gather,
                              resolve=resolve, playlist_name=playlist_name,
                              tracks_file=tracks_file, shuffle=shuffle, logger=logger,
                              extras=extras)
    outcome.published_ids = list(ids)
    outcome.plugin_reasons = plugin_reasons
    if not outcome.ok:
        print("[Workshop] ERROR: {}".format(outcome.summary()))
        if logger is not None:
            logger.error(outcome.summary(), context=DECISION_KIND)
        return outcome

    print("[Workshop] {}".format(outcome.summary()))
    if logger is not None:
        logger.decision(DECISION_KIND, outcome.summary())
    return outcome


def download_workshop_item(published_id, protocol, **kwargs):
    """One-id wrapper over ``download_workshop_items``, projected onto ``DownloadOutcome``.

    Kept because the CLI's ``DownloadOutcome`` contract and the tests written against it
    are worth more than the two lines of projection below.
    """
    published_id = str(published_id).strip()
    batch = download_workshop_items([published_id] if published_id else [], protocol,
                                    **kwargs)
    return DownloadOutcome(
        published_id=published_id,
        ok=batch.ok,
        reason=(batch.plugin_reasons.get(published_id, "") if batch.ok else batch.reason),
        detail=batch.detail,
        item_dir=batch.item_dirs.get(published_id),
        quarantine_path=batch.quarantined.get(published_id),
        validation_reasons=batch.validation_reasons.get(published_id, []),
        resolved_count=batch.resolved_count,
    )
