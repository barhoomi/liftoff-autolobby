"""Reading the shared JSONL event log.

Both producers append to ``<log_dir>/bot-YYYY-MM-DD.jsonl`` — the orchestrator via
``orchestrator/event_log.py`` and the plugin via ``plugin/EventLog.cs`` — one JSON object
per line, distinguished by ``source``. See ``docs/features/done/structured-logging.md``
for the schema contract. This module is the read half nobody had yet.

Two properties of the writers shape everything here:

- **Append-only, never rewritten.** So tailing is just "remember a byte offset", and a
  file that shrank can only mean it was rotated/replaced, not edited.
- **A new file every UTC day.** So a tail must re-derive the filename as it goes, or it
  goes silent at midnight.

Parsing is deliberately tolerant: a torn final line (a writer mid-append) or a line from
a future schema must never break the stream. Unparseable lines are surfaced as
``{"event": "_unparseable", ...}`` rather than dropped, so a corrupt log is visible in
the dashboard instead of silently invisible.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

DAILY_RE = re.compile(r"^bot-(\d{4}-\d{2}-\d{2})\.jsonl$")


def utc_now():
    return datetime.now(timezone.utc)


def daily_filename(dt):
    return "bot-{}.jsonl".format(dt.strftime("%Y-%m-%d"))


def daily_path(log_dir, dt=None):
    return os.path.join(log_dir, daily_filename(dt or utc_now()))


def list_daily_files(log_dir):
    """Every ``bot-YYYY-MM-DD.jsonl`` in ``log_dir``, newest first."""
    out = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return out
    for name in names:
        m = DAILY_RE.match(name)
        if not m:
            continue
        path = os.path.join(log_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": name, "date": m.group(1), "path": path,
                    "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: f["date"], reverse=True)
    return out


def parse_line(line, source_file=None):
    """Parse one JSONL line into an event dict, never raising."""
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except Exception:
        return {"ts": None, "source": "log", "event": "_unparseable", "raw": line[:500],
                "_file": source_file}
    if not isinstance(record, dict):
        return {"ts": None, "source": "log", "event": "_unparseable", "raw": line[:500],
                "_file": source_file}
    if source_file:
        record.setdefault("_file", source_file)
    return record


def read_events(path, limit=None):
    """All events in one daily file, oldest first; the last ``limit`` if given."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    name = os.path.basename(path)
    events = [parse_line(line, name) for line in lines]
    return [e for e in events if e]


def read_recent(log_dir, limit=200, days=2, now=None):
    """The most recent ``limit`` events, walking back up to ``days`` daily files.

    Walking back matters right after midnight (and after a bot restart at 00:0x), when
    today's file holds two lines and every interesting thing happened "yesterday".
    """
    if not log_dir or limit <= 0:
        # Guard the slice: events[-0:] is the WHOLE list, so a caller asking for zero
        # backlog (the SSE stream's "just tail from now") would get the entire day.
        return []
    now = now or utc_now()
    events = []
    for offset in range(days):
        day = now - timedelta(days=offset)
        path = daily_path(log_dir, day)
        if not os.path.exists(path):
            continue
        events = read_events(path, limit=limit) + events
        if len(events) >= limit:
            break
    return events[-limit:]


class EventTail:
    """Follower over the daily JSONL file, for the SSE stream.

    Holds a (path, offset) cursor and hands back whole lines only. Rollover to a new
    daily file and truncation/replacement of the current one are both handled by
    re-deriving the path and comparing sizes on every poll, so no inotify/watchdog
    dependency is needed for a file that gains a handful of lines a minute.
    """

    def __init__(self, log_dir, clock=utc_now):
        self.log_dir = log_dir
        self._clock = clock
        self.path = None
        self.offset = 0

    def seek_to_end(self):
        """Start streaming from *now*: skip everything already in the file."""
        self.path = daily_path(self.log_dir, self._clock())
        try:
            self.offset = os.path.getsize(self.path)
        except OSError:
            self.offset = 0
        return self

    def poll(self):
        """Return the events appended since the last call (possibly empty)."""
        if not self.log_dir:
            return []
        current = daily_path(self.log_dir, self._clock())
        if current != self.path:
            # Day rolled over: finish the old file, then start the new one at 0.
            events = self._read_from(self.path, self.offset) if self.path else []
            self.path = current
            self.offset = 0
            return events + self._read_from(current, 0)

        try:
            size = os.path.getsize(current)
        except OSError:
            return []
        if size < self.offset:  # truncated/replaced underneath us -> re-read from 0
            self.offset = 0
        if size == self.offset:
            return []
        return self._read_from(current, self.offset)

    def _read_from(self, path, offset):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return []
        if not data:
            return []
        # Only consume through the last complete line; a trailing partial line is a
        # writer mid-append and must be re-read next poll, not emitted as garbage.
        cut = data.rfind("\n")
        if cut == -1:
            return []
        consumed = data[:cut + 1]
        if path == self.path:
            self.offset = offset + len(consumed.encode("utf-8"))
        name = os.path.basename(path)
        events = [parse_line(line, name) for line in consumed.splitlines()]
        return [e for e in events if e]
