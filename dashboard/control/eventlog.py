"""Structured-event-logging adapter for the control plane.

Moved out of ``orchestrator/run_headless_lobby.py`` unchanged: the defensive import of
``event_log`` (so a partial rollout that shipped only the orchestrator script still
runs, with logging degraded to a no-op), the ``_NullLogger`` stand-in, and the
``make_event_logger`` factory. The actual logger implementation still lives in
``orchestrator/event_log.py`` — this module never re-implements the schema, the daily
filename convention or the log-dir resolution.
"""

from . import paths  # noqa: F401  (import performs the orchestrator sys.path bootstrap)

try:
    from event_log import EventLogger, resolve_log_dir  # noqa: F401
    EVENT_LOG_AVAILABLE = True
except Exception as _event_log_import_err:  # pragma: no cover - defensive
    EVENT_LOG_AVAILABLE = False
    EventLogger = None
    resolve_log_dir = None
    print(f"[Host] WARNING: event_log module unavailable ({_event_log_import_err}); "
          f"structured logging disabled.")


class NullLogger:
    """No-op stand-in with the EventLogger method surface, used when event_log is
    unavailable so call sites need no `if logger` guards for availability."""

    def _noop(self, *args, **kwargs):
        return None

    def __getattr__(self, _name):
        return self._noop


def make_event_logger(config, project_dir):
    """Build the structured event logger (or a no-op stand-in if the module is missing)."""
    if not EVENT_LOG_AVAILABLE:
        return NullLogger()
    try:
        log_dir = resolve_log_dir(config, project_dir)
        return EventLogger(log_dir)
    except Exception as e:  # pragma: no cover - defensive
        print(f"[Host] WARNING: failed to initialize structured logging ({e}); disabling.")
        return NullLogger()
