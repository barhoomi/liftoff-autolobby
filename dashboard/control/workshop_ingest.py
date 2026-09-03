"""Workshop ingest: the ONE implementation of everything that happens after a download.

See ``docs/features/doing/workshop-ingest-hardening.md`` (§2.4, §5, §7, §9). Two entry
points share every step in this module and differ in exactly one thing — how they wait:

- the **CLI** (``dashboard/control/workshop_download.py``) blocks, because a human is
  watching its exit code;
- the **auto-ingest** (``WorkshopIngest`` below) does not, because it is polled from the
  orchestrator's 1-second monitor loop, which is also the watchdog that relaunches a
  crashed game every 15 seconds.

The ingest order is the same for both, and every step of it is load-bearing::

    download(s) -> validate the SET -> quarantine rejects (+ unsubscribe)
                -> wait for a FRESH availability sweep -> gather() -> the game lists it?
                -> resolve()

Why that order rather than the obvious one:

- **Validate as a set, not per item.** Liftoff publishes a track and its race as separate
  workshop items, so item-at-a-time validation quarantines a perfectly good race item
  (live, 2026-09-03) and then the track can never validate either.
- **Sweep before gather.** ``gather_tracks_and_races()`` rebuilds the master list *only*
  from ``ui_tracks_dump.json`` when that file exists, and that dump is written by the
  plugin's Environment x GameMode sweep. Gathering against a stale dump is exactly the
  failure that made a downloaded track invisible for a month of restarts.
- **Ask whether the game lists it.** Files on disk prove nothing: the game enumerates
  content by Steam *subscription*, so "the download succeeded" and "the track is playable"
  are different claims. ``game_listing_missing`` is the one that reports the second.

The state machine is modelled on ``TrackBootstrap`` (``control/bootstrap.py``) — the
repo's existing, tested answer to "a multi-minute wait inside a 1-second loop": a
self-rate-limiting ``poll()`` with injectable ``clock``/``gather``/``resolve``. Unlike
that one, ``WorkshopIngest`` never retires; it returns to IDLE and waits for the next
``/dl``.
"""

import collections
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import paths  # noqa: F401  (import performs the orchestrator sys.path bootstrap)
from . import bootstrap
from .protocol import WORKSHOP_BUSY_FILE, WORKSHOP_CLAIM_FILE

from workshop_items import quarantine_item, workshop_content_root, workshop_item_dir  # noqa: E402

# `kind` for the `decision` events the whole workshop path emits, and the quarantine
# sub-directory / event `source` for items it rejects (workshop-steamcmd-install.md will
# use its own source, through the same quarantine_item()).
DECISION_KIND = "workshop_download"
QUARANTINE_SOURCE = "ingame_download"

# Failure reasons the control plane can observe. The plugin's own vocabulary (bad_id,
# download_rejected, queue_full, <EResult name>, timeout) passes through untouched.
REASON_BAD_ID = "bad_id"
REASON_WATCHER_TIMEOUT = "watcher_timeout"
REASON_ITEM_DIR_MISSING = "item_dir_missing"
REASON_VALIDATION_FAILED = "validation_failed"
REASON_GATHER_FAILED = "gather_failed"
REASON_SWEEP_TIMEOUT = "sweep_timeout"
REASON_GAME_LISTING_MISSING = "game_listing_missing"

# The plugin rewrites workshop_download_busy.txt every tick while anything is pending or
# queued, so a marker whose mtime stopped advancing means the plugin died mid-batch. 30s
# is ~30 missed ticks: long enough that a stalled frame or a slow disk cannot trip it,
# short enough that a crash does not wedge auto-ingest for a whole rotation.
BUSY_STALE_SECONDS = 30.0

# A CLI run refreshes its claim on every poll of its own wait, so a claim this old means
# the process holding it is gone. Generous, because a legitimate run can span a slow
# download AND a multi-minute sweep wait.
CLAIM_STALE_SECONDS = 600.0

# Floor for the sweep wait. The sweep only runs when the next rotation opens the settings
# popup, so the bound has to cover a whole rotation interval plus the multi-minute walk.
DEFAULT_SWEEP_TIMEOUT_SECONDS = 900.0
DEFAULT_ROTATION_INTERVAL_SECONDS = 600

AVAILABILITY_DUMP_FILE = bootstrap.AVAILABILITY_DUMP_FILE


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------

@dataclass
class BatchOutcome:
    """What happened to one ingest batch. ``ok`` is the only thing a caller must branch on."""

    published_ids: List[str] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    detail: Optional[str] = None
    item_dirs: Dict[str, str] = field(default_factory=dict)
    quarantined: Dict[str, str] = field(default_factory=dict)
    validation_reasons: Dict[str, List[str]] = field(default_factory=dict)
    warnings: Dict[str, List[str]] = field(default_factory=dict)
    plugin_reasons: Dict[str, str] = field(default_factory=dict)
    resolved_count: Optional[int] = None

    def summary(self):
        ids = ", ".join(self.published_ids) or "(none)"
        if self.ok:
            extra = ""
            if self.resolved_count is not None:
                extra = "; playlist now resolves to {} tracks".format(self.resolved_count)
            warned = sorted(self.warnings)
            if warned:
                extra += "; warnings on {}".format(", ".join(warned))
            return "workshop item(s) {} installed and validated{}".format(ids, extra)
        detail = ": {}".format(self.detail) if self.detail else ""
        return "workshop item(s) {} failed ({}){}".format(ids, self.reason, detail)


def fail_batch(published_ids, reason, logger, detail=None, **fields):
    outcome = BatchOutcome(published_ids=list(published_ids), ok=False, reason=reason,
                           detail=detail, **fields)
    print("[Workshop] ERROR: {}".format(outcome.summary()))
    if logger is not None:
        logger.error(outcome.summary(), context=DECISION_KIND)
    return outcome


# ---------------------------------------------------------------------------
# Injectable defaults (the same functions the orchestrator's own startup calls)
# ---------------------------------------------------------------------------

def default_gather():
    from gather_tracks import gather_tracks_and_races
    return gather_tracks_and_races()


def default_resolve(playlist_name, shuffle, tracks_file, logger=None):
    from .playlists import resolve_and_write_playlist
    return resolve_and_write_playlist(playlist_name, shuffle, tracks_file, logger=logger)


def default_validate(item_dirs, race_search_dirs=None):
    """Set-based validation, with the two ingest-specific leniencies (§7.2).

    ``require_race=False``/``require_gates=False``: a gateless "freestyle" track is
    perfectly flyable in the non-race modes the game offers it in, and *which* modes those
    are is a question the game already answers through the availability sweep — so this
    path records a warning and lets ``cross_validate_tracks`` do the mode filtering,
    instead of trackcheck growing a second mode vocabulary.
    """
    from trackcheck.validate import validate_item_set
    return validate_item_set(item_dirs, race_search_dirs=race_search_dirs,
                             require_race=False, require_gates=False)


def race_search_dirs(item_dirs, game_dir=None):
    """Where to look for the ``.race`` that matches a downloaded ``.track``.

    Every member of the batch first (a track and its race are routinely two items of the
    same ``/dl``), plus the whole workshop content root, because the partner may have been
    installed by an earlier download — and rejecting a good track for someone else's
    packaging choice is exactly the deadlock this feature exists to remove.
    """
    dirs = list(item_dirs)
    root = workshop_content_root(game_dir=game_dir)
    if root and root not in dirs:
        dirs.append(root)
    return dirs


# ---------------------------------------------------------------------------
# The availability sweep: has a FRESH one landed, and does it list our tracks?
# ---------------------------------------------------------------------------

def availability_dump_mtime(plugins_dir):
    """mtime of ``track_mode_availability.json``, or 0.0 when it is absent."""
    try:
        return os.path.getmtime(os.path.join(plugins_dir, AVAILABILITY_DUMP_FILE))
    except OSError:
        return 0.0


def load_availability(plugins_dir):
    """Parsed availability dump, or None when absent/half-written."""
    return bootstrap._load_json(os.path.join(plugins_dir, AVAILABILITY_DUMP_FILE))


def _names_match(a, b):
    """The matching rule ``gather_tracks.py`` already uses: case-insensitive exact, then
    substring in either direction. Deliberately not a third, cleverer rule."""
    a, b = (a or "").lower(), (b or "").lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def track_listed(availability, name, environment):
    """Is ``name`` offered by the game in ``environment``, in any mode?"""
    if not isinstance(availability, dict) or not name:
        return False
    from trackcheck.parser import normalize_env
    wanted_env = normalize_env(environment) if environment else None
    for env_key, modes in availability.items():
        if wanted_env and normalize_env(env_key) != wanted_env:
            continue
        if not isinstance(modes, dict):
            continue
        for listed in modes.values():
            if not isinstance(listed, list):
                continue
            if any(_names_match(name, entry) for entry in listed):
                return True
    return False


def sweep_satisfied(plugins_dir, baseline_mtime, tracks):
    """True once a *usable* availability sweep newer than the ingest has landed.

    Two conditions, and the second is a disjunction on purpose:

    - ``track_dump_ready`` — both dumps parse and at least one environment has tracks.
      Never mtime alone: the plugin writes them with ``File.WriteAllLines``, not
      atomically, so a fresh mtime on a half-written file is a real observation.
    - the dump is newer than the baseline **or** it already lists every track we
      ingested. The second disjunct closes the race where a rotation's sweep lands between
      the plugin writing the result and the reader capturing its baseline — without it,
      an ingest that already succeeded would sit and wait out its timeout.
    """
    if not bootstrap.track_dump_ready(plugins_dir):
        return False
    if availability_dump_mtime(plugins_dir) > baseline_mtime:
        return True
    if not tracks:
        return False
    availability = load_availability(plugins_dir)
    return all(track_listed(availability, name, env) for name, env in tracks)


def sweep_timeout_seconds(protocol, override=None):
    """``max(900s, 2 x rotation_interval)`` — the sweep only runs when the next rotation
    opens the settings popup, so the bound must cover a full interval plus the walk."""
    if override is not None:
        return float(override)
    interval = DEFAULT_ROTATION_INTERVAL_SECONDS
    if protocol is not None:
        interval = protocol.read_int("rotation_interval.txt",
                                     DEFAULT_ROTATION_INTERVAL_SECONDS)
        if not interval:
            interval = DEFAULT_ROTATION_INTERVAL_SECONDS
    return max(DEFAULT_SWEEP_TIMEOUT_SECONDS, 2.0 * float(interval))


def sweep_blocked_detail(protocol, elapsed=None):
    """Why a sweep may legitimately never have run. Both of these are operator state, not
    a bug: with rotation paused or disengaged the settings popup never re-opens, and the
    sweep only ever happens inside that popup."""
    paused = protocol.read_flag("rotation_paused.txt") if protocol is not None else None
    engaged = protocol.read_text("rotation_engaged.txt", "true") if protocol is not None else None
    prefix = "no fresh availability sweep"
    if elapsed is not None:
        prefix += " after {:.0f}s".format(elapsed)
    return ("{} -- the sweep only runs when a rotation re-opens the settings popup "
            "(rotation_paused={} rotation_engaged={})".format(prefix, paused, engaged))


def tracks_to_confirm(reports):
    """``[(track name, environment)]`` for the members that actually carry a track.

    Race-only items are skipped: a ``.race`` declares no environment and the game does not
    list races in the track dropdown at all, so there is nothing to confirm for them.
    """
    tracks = []
    for report in reports:
        if getattr(report, "track_path", None) and report.name and report.environment:
            tracks.append((report.name, report.environment))
    return tracks


# ---------------------------------------------------------------------------
# The shared core: validate + quarantine, then finalize
# ---------------------------------------------------------------------------

def validate_and_quarantine_batch(item_dirs, race_search_dirs=None, validate=None,
                                  quarantine=None, protocol=None, project_dir=None,
                                  logger=None):
    """Validate ``{published_id: item_dir}`` as a set; quarantine only the members that fail.

    Returns ``(surviving, reports)``, both keyed by published id and in input order.

    Quarantine policy (§5.4): a member is never punished for a sibling's failure, and a
    rejected member is moved aside — never deleted — because the operator needs the files
    to diagnose the rejection. Immediately after a successful quarantine, the id is queued
    for ``SteamUGC.UnsubscribeItem``: without that, Steam re-downloads the subscribed item
    back into the content root and the next availability sweep lists it, which would put a
    track that FAILED validation into the rotation — the exact outcome this path exists to
    prevent. A failure to write that request is logged and swallowed, like the quarantine
    failure itself: neither may mask the rejection.
    """
    validate = validate or default_validate
    quarantine = quarantine or quarantine_item

    dirs = list(item_dirs.values())
    if race_search_dirs is None:
        race_search_dirs = list(dirs)
    by_dir = validate(dirs, race_search_dirs=race_search_dirs)

    surviving = collections.OrderedDict()
    reports = collections.OrderedDict()
    quarantined = {}
    rejected_reasons = {}
    warnings = {}
    to_unsubscribe = []

    for published_id, item_dir in item_dirs.items():
        report = by_dir.get(item_dir)
        reports[published_id] = report
        if report is None:  # a validator that skipped a member is a bug, not a pass
            rejected_reasons[published_id] = ["NO_REPORT"]
            continue

        # trackcheck reasons are a str-Enum: `.value` is the bare "GATE_DATA_MISSING"
        # code, while str() would render "Reason.GATE_DATA_MISSING". The bare code is what
        # the quarantine manifest, the JSONL event and any grep want.
        warned = [getattr(r, "value", str(r)) for r in getattr(report, "warnings", [])]
        if warned:
            warnings[published_id] = warned
            message = "{} ingested with warnings: {}".format(published_id, ", ".join(warned))
            print("[Workshop] {}".format(message))
            if logger is not None:
                logger.decision(DECISION_KIND, message)

        if report.ok:
            surviving[published_id] = item_dir
            continue

        reasons = [getattr(r, "value", str(r)) for r in report.reasons]
        rejected_reasons[published_id] = reasons
        try:
            manifest = quarantine(item_dir, reasons, QUARANTINE_SOURCE,
                                  project_dir=project_dir, logger=logger,
                                  published_id=published_id)
            quarantined[published_id] = manifest.get("quarantine_path")
        except Exception as exc:  # quarantine failing must not mask the rejection
            print("[Workshop] ERROR: failed to quarantine {}: {}".format(item_dir, exc))
            if logger is not None:
                logger.error("failed to quarantine rejected workshop item {}: {}".format(
                    published_id, exc), context=DECISION_KIND)
            continue

        to_unsubscribe.append(published_id)

    # ONE request for the whole batch, after the loop -- not one per member as the feature
    # doc's §1.4 wording suggests. workshop_unsubscribe_request.txt is a one-shot file the
    # plugin reads and deletes on its 1s tick, so two writes microseconds apart would mean
    # the second silently replaces the first and the first id is never unsubscribed. The
    # file's own format (up to 16 ids, one per line) is what this batching is for. Failure
    # is logged and swallowed: it must never mask the rejection it accompanies.
    if protocol is not None and to_unsubscribe:
        try:
            protocol.request_workshop_unsubscribe(to_unsubscribe)
        except Exception as exc:
            print("[Workshop] ERROR: failed to request unsubscribe of {}: {}".format(
                ", ".join(to_unsubscribe), exc))
            if logger is not None:
                logger.error("failed to request unsubscribe of {}: {}".format(
                    ", ".join(to_unsubscribe), exc), context=DECISION_KIND)

    return surviving, reports, {
        "quarantined": quarantined,
        "rejected_reasons": rejected_reasons,
        "warnings": warnings,
    }


def finalize_ingest(surviving, reports, plugins_dir, gather=None, resolve=None,
                    playlist_name=None, tracks_file=None, shuffle=False, logger=None,
                    extras=None):
    """gather -> "does the game list it?" -> resolve. Returns the batch's ``BatchOutcome``.

    ``gather()`` first, because it is what turns a validated item into a rotation
    candidate, and it can only see the item if the fresh dump already does — hence the
    caller's sweep wait. The listing check afterwards is what separates "Steam downloaded
    it" from "the game will offer it": on ``game_listing_missing`` the files are fine and
    are deliberately NOT quarantined.
    """
    gather = gather or default_gather
    resolve = resolve or default_resolve
    extras = extras or {}
    published_ids = list(surviving)

    outcome = BatchOutcome(
        published_ids=published_ids,
        ok=False,
        item_dirs=dict(surviving),
        quarantined=dict(extras.get("quarantined", {})),
        validation_reasons=dict(extras.get("rejected_reasons", {})),
        warnings=dict(extras.get("warnings", {})),
    )

    try:
        gather()
    except Exception as exc:
        outcome.reason = REASON_GATHER_FAILED
        outcome.detail = str(exc)
        return outcome

    availability = load_availability(plugins_dir)
    missing = []
    for published_id in published_ids:
        report = reports.get(published_id)
        if report is None:
            continue
        for name, env in tracks_to_confirm([report]):
            if not track_listed(availability, name, env):
                missing.append("{} ('{}' in {})".format(published_id, name, env))
    if missing:
        outcome.reason = REASON_GAME_LISTING_MISSING
        outcome.detail = ("the refreshed availability dump does not list {} -- the files "
                          "are on disk and were NOT quarantined".format("; ".join(missing)))
        return outcome

    if playlist_name and tracks_file:
        try:
            resolved = resolve(playlist_name, shuffle, tracks_file, logger=logger)
        except Exception as exc:
            outcome.reason = REASON_GATHER_FAILED
            outcome.detail = str(exc)
            return outcome
        outcome.resolved_count = len(resolved or [])

    outcome.ok = True
    return outcome


# ---------------------------------------------------------------------------
# The non-blocking state machine
# ---------------------------------------------------------------------------

class WorkshopIngest:
    """Ingest a ``/dl`` nobody is waiting for, from inside the 1-second monitor loop.

    A chat ``/dl <track_id> <race_id>`` makes the plugin write a
    ``workshop_download_result.txt`` that no CLI is watching for. This machine notices
    that, and runs the same validate -> sweep-wait -> gather -> resolve path the CLI runs
    — as a **poll-driven state machine**, never a wait. That distinction is the whole
    design: the loop this lives in is also the watchdog that relaunches a crashed game
    every 15 seconds, so ``poll()`` must never sleep, never block on a subprocess, and
    never do more than a few stats and small reads per call. (``gather()`` in FINALIZING is
    the one genuinely slow call, once per ingest cycle — the same cost ``TrackBootstrap``
    already pays in this loop.)

    Permanent, unlike ``TrackBootstrap``: it returns to IDLE after every cycle rather than
    retiring.
    """

    IDLE = "idle"                    # nothing in flight; watching for a result file
    COLLECTING = "collecting"        # a batch is arriving; the plugin is still busy
    WAITING_SWEEP = "waiting_sweep"  # validated; waiting for a fresh availability sweep
    FINALIZING = "finalizing"        # sweep landed; gather + listing check + resolve

    def __init__(self, protocol, plugins_dir, game_dir=None, playlist_name=None,
                 tracks_file=None, shuffle=False, project_dir=None, logger=None,
                 poll_interval=2.0, sweep_timeout=None, clock=time.monotonic,
                 gather=None, resolve=None, validate=None, quarantine=None,
                 resolve_item_dir=None):
        self.protocol = protocol
        self.plugins_dir = plugins_dir
        self.game_dir = game_dir
        self.playlist_name = playlist_name
        self.tracks_file = tracks_file
        self.shuffle = shuffle
        self.project_dir = project_dir
        self.logger = logger
        self.poll_interval = poll_interval
        self.sweep_timeout = sweep_timeout
        self._clock = clock
        self._gather = gather or default_gather
        self._resolve = resolve or default_resolve
        self._validate = validate or default_validate
        self._quarantine = quarantine or quarantine_item
        self._resolve_item_dir = resolve_item_dir or workshop_item_dir

        self.state = self.IDLE
        self.last_outcome = None
        self._batch = collections.OrderedDict()   # published_id -> plugin record
        self._deferred = collections.deque()
        self._surviving = collections.OrderedDict()
        self._reports = {}
        self._extras = {}
        self._baseline_mtime = 0.0
        self._deadline = None
        self._last_poll = None

    # --- state ---------------------------------------------------------------

    @property
    def active(self):
        """True while a cycle is in progress. Never False forever: this machine is
        permanent, so 'inactive' just means 'waiting for the next /dl'."""
        return self.state != self.IDLE

    # --- driving -------------------------------------------------------------

    def poll(self, force=False):
        """Advance at most one step. Cheap and safe to call on every loop tick."""
        now = self._clock()
        if not force and self._last_poll is not None and \
                (now - self._last_poll) < self.poll_interval:
            return self.state
        self._last_poll = now

        try:
            if self.state == self.IDLE:
                return self._poll_idle()
            if self.state == self.COLLECTING:
                return self._poll_collecting()
            if self.state == self.WAITING_SWEEP:
                return self._poll_waiting_sweep(now)
            if self.state == self.FINALIZING:
                return self._poll_finalizing()
        except Exception as exc:  # never raise into the monitor loop
            print("[Ingest] ERROR: auto-ingest poll failed: {}".format(exc))
            if self.logger is not None:
                self.logger.error("workshop auto-ingest poll failed: {}".format(exc),
                                  context=DECISION_KIND)
            self._reset()
        return self.state

    # --- states --------------------------------------------------------------

    def _poll_idle(self):
        if self._deferred:
            self._start_batch(self._deferred.popleft())
            return self.state

        record = self._take_unclaimed_result()
        if record is None:
            return self.state
        self._start_batch(record)
        return self.state

    def _start_batch(self, record):
        self._batch = collections.OrderedDict()
        self._batch[record["published_id"]] = record
        self._baseline_mtime = availability_dump_mtime(self.plugins_dir)
        self._deadline = self._clock() + self._sweep_timeout()
        self.state = self.COLLECTING
        print("[Ingest] Unclaimed workshop download result for {} -- ingesting.".format(
            record["published_id"]))

    def _poll_collecting(self):
        while True:
            record = self._take_unclaimed_result()
            if record is None:
                break
            self._batch[record["published_id"]] = record

        if self._plugin_busy():
            return self.state

        landed = collections.OrderedDict()
        for published_id, record in self._batch.items():
            if not record["ok"]:
                print("[Ingest] {} failed in the plugin ({}) -- nothing to validate.".format(
                    published_id, record["reason"] or "unknown"))
                continue
            item_dir = self._resolve_item_dir(published_id, game_dir=self.game_dir)
            if not item_dir:
                print("[Ingest] {} reported ok but no item directory exists.".format(
                    published_id))
                continue
            landed[published_id] = item_dir

        if not landed:
            ids = list(self._batch)
            failures = [r["reason"] for r in self._batch.values() if not r["ok"]]
            # All the plugin's own fault -> report the plugin's own reason verbatim;
            # otherwise Steam said ok and the files are not there, which is its own thing.
            reason = (failures[0] or "unknown") if len(failures) == len(ids) else \
                REASON_ITEM_DIR_MISSING
            self._finish(fail_batch(ids, reason, self.logger,
                                    detail="no member of the batch left usable files"))
            return self.state

        surviving, reports, extras = validate_and_quarantine_batch(
            landed, race_search_dirs=race_search_dirs(list(landed.values()),
                                                      game_dir=self.game_dir),
            validate=self._validate, quarantine=self._quarantine, protocol=self.protocol,
            project_dir=self.project_dir, logger=self.logger)

        if not surviving:
            self._finish(fail_batch(
                list(landed), REASON_VALIDATION_FAILED, self.logger,
                detail=", ".join(sorted(
                    r for reasons in extras["rejected_reasons"].values() for r in reasons)),
                quarantined=dict(extras["quarantined"]),
                validation_reasons=dict(extras["rejected_reasons"]),
                warnings=dict(extras["warnings"])))
            return self.state

        self._surviving, self._reports, self._extras = surviving, reports, extras
        self.state = self.WAITING_SWEEP
        print("[Ingest] {} validated -- waiting for the plugin's availability re-sweep.".format(
            ", ".join(surviving)))
        return self.state

    def _defer_arrivals(self):
        """Take results that land while a cycle is already past COLLECTING off disk.

        Consumed, not left in place: the plugin writes ONE result file, so a third result
        arriving during a multi-minute sweep wait would overwrite the second. They are
        never merged into a batch whose validation has already run — they start the next
        cycle instead.
        """
        while True:
            record = self._take_unclaimed_result()
            if record is None:
                return
            print("[Ingest] Holding a result for {} until the current ingest finishes.".format(
                record["published_id"]))
            self._deferred.append(record)

    def _poll_waiting_sweep(self, now):
        self._defer_arrivals()
        tracks = tracks_to_confirm([self._reports[i] for i in self._surviving])
        if sweep_satisfied(self.plugins_dir, self._baseline_mtime, tracks):
            self.state = self.FINALIZING
            return self.state
        if self._deadline is not None and now >= self._deadline:
            self._finish(fail_batch(
                list(self._surviving), REASON_SWEEP_TIMEOUT, self.logger,
                detail=sweep_blocked_detail(self.protocol),
                item_dirs=dict(self._surviving),
                quarantined=dict(self._extras.get("quarantined", {})),
                warnings=dict(self._extras.get("warnings", {}))))
        return self.state

    def _poll_finalizing(self):
        self._defer_arrivals()
        outcome = finalize_ingest(
            self._surviving, self._reports, self.plugins_dir,
            gather=self._gather, resolve=self._resolve,
            playlist_name=self._active_playlist(), tracks_file=self.tracks_file,
            shuffle=self.shuffle, logger=self.logger, extras=self._extras)
        self._finish(outcome)
        return self.state

    # --- internals -----------------------------------------------------------

    def _active_playlist(self):
        """Read at FINALIZING time, never held from construction: a playlist switched
        between the /dl and the sweep is then honoured for free, and the machine never
        holds a stale copy of loop state. "" and "custom" both mean "refresh the track
        database but rewrite no rotation file" -- the rule the CLI already applies."""
        name = self.playlist_name
        if name is None and self.protocol is not None:
            name = self.protocol.read_text("playlist_name.txt", "") or ""
        if name in (None, "", "custom"):
            return None
        return name

    def _sweep_timeout(self):
        return sweep_timeout_seconds(self.protocol, override=self.sweep_timeout)

    def _claimed_ids(self):
        """Ids a blocking CLI run is driving. A claim older than CLAIM_STALE_SECONDS is
        ignored AND deleted: a CLI killed mid-run must not disable auto-ingest forever."""
        ids, mtime = self.protocol.read_workshop_download_claim()
        if mtime is None:
            return set()
        if (time.time() - mtime) > CLAIM_STALE_SECONDS:
            print("[Ingest] Ignoring a stale {} ({} ids) and deleting it.".format(
                WORKSHOP_CLAIM_FILE, len(ids)))
            self.protocol.release_workshop_downloads()
            return set()
        return set(ids)

    def _take_unclaimed_result(self):
        """Consume the result file, unless it is half-written or claimed by the CLI.

        Reads NON-destructively first: consuming a claimed result would make the CLI
        report a false watcher_timeout while this machine quietly succeeded — the most
        confusing possible way to violate "verify before you claim".
        """
        if not self.protocol.exists("workshop_download_result.txt"):
            return None
        record = self.protocol.read_workshop_download_result()
        if record is None:
            return None  # half-written: not ready, not failed
        if record["published_id"] in self._claimed_ids():
            return None
        return self.protocol.consume_workshop_download_result()

    def _plugin_busy(self):
        try:
            mtime = os.path.getmtime(os.path.join(self.plugins_dir, WORKSHOP_BUSY_FILE))
        except OSError:
            return False
        return (time.time() - mtime) <= BUSY_STALE_SECONDS

    def _finish(self, outcome):
        self.last_outcome = outcome
        print("[Ingest] {}".format(outcome.summary()))
        if self.logger is not None:
            self.logger.decision(DECISION_KIND, outcome.summary())
        self._reset()

    def _reset(self):
        self.state = self.IDLE
        self._batch = collections.OrderedDict()
        self._surviving = collections.OrderedDict()
        self._reports = {}
        self._extras = {}
        self._deadline = None
        self._baseline_mtime = 0.0
