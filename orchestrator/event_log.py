"""Structured JSONL event logging — orchestrator half of `structured-logging`.

Both the C# plugin (ROADMAP task A3, future) and this orchestrator append
newline-delimited JSON events into the *same* daily file
``<log_dir>/bot-YYYY-MM-DD.jsonl``. Each record carries a ``source`` field
(``"orchestrator"`` | ``"plugin"``) so the two producers never need to coordinate
beyond agreeing on the directory + filename convention (see
``docs/features/doing/structured-logging.md`` for the canonical schema).

Design notes:
- Writes use append mode (POSIX ``O_APPEND``); each event is a single
  newline-terminated line. POSIX guarantees append writes below ``PIPE_BUF``
  (4096 bytes on Linux) are atomic, and events are far smaller, so concurrent
  appends from separate processes interleave cleanly without a lock and without
  corrupting each other.
- The file is (re)opened per event. This keeps day-rollover correctness trivial
  (the filename is recomputed from the event's own timestamp every time) and is
  cheap given the orchestrator only emits a handful of low-frequency events.
- Emitting an event must NEVER crash the orchestrator control loop; all IO is
  wrapped and failures degrade to a warning on stdout.
"""

import json
import os
from datetime import datetime, timezone

# Value written into every orchestrator-produced record's ``source`` field. The
# plugin half uses "plugin". This is the key that lets both halves share one file.
SOURCE_ORCHESTRATOR = "orchestrator"

# Timestamp format shared by both halves (see feature doc). ISO-8601, UTC, second
# precision, Zulu suffix, no fractional seconds.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_DATE_FORMAT = "%Y-%m-%d"


def utc_now():
    """Timezone-aware 'now' in UTC. Single source of the clock so tests can stub it."""
    return datetime.now(timezone.utc)


def format_ts(dt):
    """Format a datetime as the canonical ISO-8601 UTC second-precision timestamp."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(_TS_FORMAT)


def daily_filename(dt):
    """Return the bare daily filename (``bot-YYYY-MM-DD.jsonl``) for the given instant."""
    return "bot-{}.jsonl".format(dt.strftime(_DATE_FORMAT))


def repo_root():
    """Absolute path to the repository root (parent of orchestrator/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_log_dir(config=None, project_dir=None):
    """Resolve the log directory with precedence (first hit wins):

    1. env ``FPV_LOG_DIR`` (Docker volume-mount hook),
    2. ``lobby_config.json`` key ``log_dir`` (absolute used as-is; relative resolved
       against the repo root / ``project_dir``),
    3. default ``<repo_root>/logs``.

    No absolute paths are hardcoded so the same code works on the host and in a
    container with a mounted volume.
    """
    base = project_dir or repo_root()

    env_dir = os.environ.get("FPV_LOG_DIR")
    if env_dir:
        return env_dir

    if config:
        configured = config.get("log_dir")
        if configured:
            if os.path.isabs(configured):
                return configured
            return os.path.join(base, configured)

    return os.path.join(base, "logs")


class EventLogger:
    """Appends structured JSONL events to the shared daily log file.

    Parameters
    ----------
    log_dir : str
        Directory the daily files live in (see :func:`resolve_log_dir`).
    source : str
        Value for each record's ``source`` field. Defaults to ``"orchestrator"``.
    clock : callable, optional
        Zero-arg callable returning a timezone-aware ``datetime`` (defaults to
        :func:`utc_now`). Injected in tests to exercise day rollover deterministically.
    """

    def __init__(self, log_dir, source=SOURCE_ORCHESTRATOR, clock=utc_now):
        self.log_dir = log_dir
        self.source = source
        self._clock = clock

    def current_path(self, now=None):
        """Absolute path of the daily file for ``now`` (or the current instant)."""
        now = now or self._clock()
        return os.path.join(self.log_dir, daily_filename(now))

    def emit(self, event, **fields):
        """Write one event line and return the record dict.

        The envelope (``ts``, ``source``, ``event``) comes first, then any non-None
        payload fields in the order given. ``None`` fields are omitted. Returns the
        record even if the write fails (so callers/tests can inspect it); write
        failures degrade to a stdout warning and never raise.
        """
        now = self._clock()
        record = {
            "ts": format_ts(now),
            "source": self.source,
            "event": event,
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = value

        line = json.dumps(record, ensure_ascii=False)
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            # Append mode == O_APPEND: atomic small-line appends, safe to co-write
            # with the plugin. Filename recomputed from `now` => day rollover is free.
            with open(self.current_path(now), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # never let logging break the control loop
            print("[EventLog] WARNING: failed to write event '{}': {}".format(event, exc))
        return record

    # --- Typed helpers for the orchestrator event catalogue (see feature doc) ---

    def orchestrator_start(self, interval, playlist=None, lobby_name=None,
                           gui=None, auto_start=None):
        return self.emit("orchestrator_start", interval=interval, playlist=playlist,
                         lobby_name=lobby_name, gui=gui, auto_start=auto_start)

    def game_start(self, pid, playlist=None, width=None, height=None):
        return self.emit("game_start", pid=pid, playlist=playlist,
                         width=width, height=height)

    def playlist_resolved(self, playlist, track_count, shuffle=None,
                          dropped_missing=None, dropped_mode=None, fallback=None):
        return self.emit("playlist_resolved", playlist=playlist,
                         track_count=track_count, shuffle=shuffle,
                         dropped_missing=dropped_missing, dropped_mode=dropped_mode,
                         fallback=fallback)

    def playlist_change(self, from_playlist, to_playlist):
        # keys are "from"/"to" per schema; they are Python keywords so pass via emit()
        return self.emit("playlist_change", **{"from": from_playlist, "to": to_playlist})

    def shutdown(self, reason):
        return self.emit("shutdown", reason=reason)

    def decision(self, kind, detail):
        """A notable orchestrator-side decision, same ``kind``/``detail`` shape the
        plugin half already emits (see the feature doc's plugin catalogue). Used by the
        first-run track bootstrap (``dashboard/control/bootstrap.py``) to record that it
        armed and that it completed."""
        return self.emit("decision", kind=kind, detail=detail)

    def error(self, message, context=None, playlist=None):
        return self.emit("error", message=message, context=context, playlist=playlist)
