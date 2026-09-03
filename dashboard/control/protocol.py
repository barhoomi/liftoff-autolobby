"""The plugin ⇄ control-plane protocol files — one implementation of every write.

The plugin polls a directory of plain-text files under ``BepInEx/plugins/`` on its
1-second tick (see AGENTS.md, "Plugin ⇄ orchestrator protocol is plain text files").
Historically the orchestrator opened those files inline in ``main()``. Now there are
**two** writers — the orchestrator and the dashboard — so every write funnels through
this module, which adds the three things that inline ``open(..., "w")`` calls cannot
give us:

1. **Ownership enforcement.** ``RESET_ONLY`` files are plugin-owned runtime state
   (``rotation_state.txt``, ``shuffle_order.txt``): the plugin writes and self-heals
   them, and the control plane may only *reset* them. Any attempt to content-write one
   raises ``ProtocolOwnershipError`` instead of silently creating the second source of
   truth that AGENTS.md rule 4 exists to prevent.
2. **Atomic writes.** Writes are temp-file + ``os.replace`` (an atomic rename within the
   same directory), so the plugin's 1-second poll can never observe a half-written
   ``tracks_to_rotate.txt``. The old truncate-then-write left a window where it could.
3. **Mutual exclusion between writers.** All writes take an ``fcntl.flock`` on
   ``<plugins_dir>/.control.lock``. The orchestrator and the dashboard are separate
   processes, so an in-process lock would not help; a POSIX advisory file lock does, and
   costs nothing because both go through this module.

Reads are deliberately *not* locked: they are single ``read()`` calls against files that
are only ever replaced atomically, so a reader either sees the old file or the new one.
"""

import errno
import os
import re

try:  # POSIX only; the bot runs on Linux, but keep the module importable anywhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from contextlib import contextmanager

# Files the control plane owns and may write freely. Value = short purpose, used by the
# dashboard's diagnostics view so the table is documentation as well as a guard.
WRITABLE = {
    "lobby_name.txt": "Room name the bot creates.",
    "rotation_interval.txt": "Seconds between track rotations.",
    "room_private.txt": "'true' = private room, 'false' = public.",
    "auto_start.txt": "'true' = auto-click Start Race once players are in.",
    "shuffle_mode.txt": "'true' = plugin shuffles the static rotation order.",
    "democracy_mode.txt": "'true' = non-admins may vote to /skip.",
    "max_players.txt": "Max players allowed in the room.",
    "playlist_name.txt": "Active playlist name (the orchestrator re-resolves on change).",
    "available_playlists.txt": "Newline-separated playlist names, for the /playlist command.",
    "tracks_to_rotate.txt": "The static, authoritative rotation (TrackName,Env,Mode per line).",
    "log_dir.txt": "Absolute path of the shared JSONL log directory, handed to the plugin.",
    "maintenance_active.txt": "Present = maintenance shutdown scheduled (plugin cancels by deleting).",
    "override_game_mode.txt": "Forces a single game mode; absent = per-track mode.",
    "rotation_paused.txt": "'true' = rotation timer paused.",
    "rotation_engaged.txt": "'false' = rotation disengaged (absent = engaged).",
    "admin_ids.txt": "Newline-separated Photon user IDs allowed to run admin commands.",
    "keep_alive_seconds.txt": "Idle-kick avoidance interval.",
    "skip_now.txt": "Present = admin-requested immediate rotation; the plugin deletes it "
                     "on consumption (bot-dashboard.md skip-now fix), so this is a "
                     "one-shot request, not a toggled flag.",
    "say_<seq>.txt": "Sequenced operator chat message (dashboard-chat-send.md), e.g. "
                      "'say_1.txt', 'say_2.txt'. Write via enqueue_say(), never directly -- "
                      "the sequence number is assigned under the control lock by scanning "
                      "existing say_*.txt files, so two writers can never collide. The "
                      "plugin consumes files in ascending numeric order on its 1s tick, "
                      "sends the content verbatim as plain chat text, and deletes each file "
                      "after sending (same one-shot, plugin-deletes convention as "
                      "skip_now.txt). This is a documentation-only entry, not a literal "
                      "filename -- see enqueue_say()'s docstring for the real key format.",
    "workshop_download_request.txt": "One Steam Workshop published-file id (decimal) for the "
                                     "plugin to download in-game; the plugin deletes it the "
                                     "instant it starts processing, so it is a one-shot "
                                     "request like skip_now.txt (workshop-ingame-download.md).",
    "workshop_unsubscribe_request.txt": "Published-file ids (decimal, one per line, max 16) the "
                                        "plugin should SteamUGC.UnsubscribeItem. Written after a "
                                        "rejected item is quarantined, so Steam cannot silently "
                                        "re-download it and the next availability sweep cannot "
                                        "list it (workshop-ingest-hardening.md 1.4). One-shot: "
                                        "the plugin deletes it before acting.",
    "workshop_download_claim.txt": "Control-plane INTERNAL; the plugin never reads it. The "
                                   "published-file ids a blocking CLI run is currently driving, "
                                   "one per line, so the orchestrator's auto-ingest leaves those "
                                   "results for the CLI instead of racing it for the single "
                                   "result file (workshop-ingest-hardening.md 9.4). Its mtime is "
                                   "refreshed on every CLI poll; a stale one is ignored and "
                                   "deleted, so a killed CLI cannot disable auto-ingest.",
}

# Names the two workshop features share between the plugin, the CLI and the auto-ingest.
# Kept as constants so no caller ever spells one of them by hand.
WORKSHOP_REQUEST_FILE = "workshop_download_request.txt"
WORKSHOP_RESULT_FILE = "workshop_download_result.txt"
WORKSHOP_UNSUBSCRIBE_REQUEST_FILE = "workshop_unsubscribe_request.txt"
WORKSHOP_CLAIM_FILE = "workshop_download_claim.txt"
WORKSHOP_BUSY_FILE = "workshop_download_busy.txt"

# Matches plugin/WorkshopDownloader.cs's MaxUnsubscribeIdsPerRequest: a request longer
# than this is junk, not a batch, and the plugin ignores the overflow anyway.
MAX_UNSUBSCRIBE_IDS = 16

# Plugin-owned runtime state. The plugin writes these; the control plane may ONLY reset
# them (rotation_state.txt -> "0", shuffle_order.txt -> deleted), and only from the one
# call site that already resets both: a fresh playlist resolution.
RESET_ONLY = {
    "rotation_state.txt": "Rotation cursor (plugin-owned; control plane may only reset to 0).",
    "shuffle_order.txt": "Derived shuffle deal (plugin-owned; control plane may only clear).",
}

# Plugin-produced data the control plane reads and must never write. One of them
# (workshop_download_result.txt) is nonetheless *deleted* by the control plane, through the
# single sanctioned consume_workshop_download_result() below -- the same shape as
# rotation_state.txt's reset: plugin owns the content, the control plane owns exactly one
# documented mutation of it.
READ_ONLY = {
    "track_mode_availability.json": "Ground-truth (environment, mode) -> tracks dump from the game UI.",
    "ui_tracks_dump.json": "Flat environment -> tracks dump from the game UI. The only way "
                           "official (asset-bundled) tracks are ever discovered, so it is what "
                           "gather_tracks.py reconciles master_tracks_list.json from, and what "
                           "the first-run bootstrap waits for (control/bootstrap.py).",
    "workshop_download_result.txt": "Outcome of an in-game workshop download, written by the "
                                    "plugin as '<id>|<ok|fail>|<reason>'. Consumed (read then "
                                    "deleted) via consume_workshop_download_result().",
    "workshop_download_busy.txt": "Batch-boundary heartbeat: rewritten by the plugin on EVERY "
                                  "tick while any download is pending or queued (content: the "
                                  "outstanding count), deleted when none is. Absent OR stale "
                                  "means a '/dl <track> <race>' batch is finished and safe to "
                                  "validate as a set (workshop-ingest-hardening.md 4.1) -- the "
                                  "refreshed mtime is what distinguishes 'still downloading' "
                                  "from 'left behind by a process that died'.",
}

LOCK_FILENAME = ".control.lock"

# dashboard-chat-send.md: sequenced operator chat message files, "say_<n>.txt" (n >= 1).
# Matches the plugin's own consumer-side pattern (Plugin.GameRoom.cs).
SAY_FILE_RE = re.compile(r"^say_(\d+)\.txt$")

# Sane upper bound on one operator-typed chat message. The plugin's SendChatMessage
# already splits anything over CHAT_MAX_CHARS (220) into multiple tag-safe chat lines, so
# this isn't about the game's per-line limit -- it's about not letting one operator
# request turn into an unbounded wall of chat spam. ~10 chat lines' worth is generous for
# an interactive "reply in context" use case.
MAX_SAY_MESSAGE_LENGTH = 2000


def parse_track_line(line):
    """Parse one ``tracks_to_rotate.txt`` line ("TrackName, Environment, GameMode")
    into ``(track, environment, mode)``.

    Rightmost-split into exactly 3 fields: the last comma-separated field is the game
    mode, the second-to-last is the environment, and everything remaining (rejoined
    verbatim with ``,``, so any commas/spaces inside the track name survive
    byte-for-byte) is the track name. Environments and modes are fixed vocabularies
    that never themselves contain a comma, so this is unambiguous. For a line with 3
    or fewer comma-separated fields this is identical to a plain ``split(",")``, so
    every existing (comma-free-name) file parses the same as before.

    Mirrors the plugin's ``ParseTrackLine`` (plugin/Plugin.Rotation.cs) -- this is the
    Python-side twin of that fix. Fixes a live bug: a real Liftoff track named
    "Iceberg, Right ahead!" sheared its own fields under the old left-to-right split.
    See docs/features/doing/bug-comma-in-track-name.md.
    """
    parts = line.split(",")
    if len(parts) <= 3:
        track = parts[0].strip() if parts else ""
        environment = parts[1].strip() if len(parts) > 1 else ""
        mode = parts[2].strip() if len(parts) > 2 else ""
        return track, environment, mode
    mode = parts[-1].strip()
    environment = parts[-2].strip()
    track = ",".join(parts[:-2]).strip()
    return track, environment, mode


def parse_workshop_download_result(raw):
    """Parse one ``workshop_download_result.txt`` line into
    ``{"published_id", "ok", "reason"}``, or None if it isn't a complete record yet.

    Format (written by ``plugin/WorkshopDownloader.cs``):
    ``<published_file_id>|<ok|fail>|<reason>``, reason empty on a plain success.

    Returning None for anything malformed is deliberate and load-bearing: the plugin
    writes this file with ``File.WriteAllText`` (not an atomic replace), so a poller can
    genuinely observe a half-written line. "Not parseable" therefore means "not ready
    yet, look again next tick" -- never "failed". A real failure always arrives as a
    complete ``<id>|fail|<reason>`` line.
    """
    if raw is None:
        return None
    parts = raw.strip().split("|", 2)
    if len(parts) != 3:
        return None
    published_id, status, reason = (p.strip() for p in parts)
    if not published_id or status not in ("ok", "fail"):
        return None
    return {"published_id": published_id, "ok": status == "ok", "reason": reason}


class ProtocolOwnershipError(RuntimeError):
    """Raised on an attempt to write a file the control plane does not own."""


def _bool_text(value):
    return "true" if value else "false"


class ProtocolDir:
    """Typed access to the protocol files in one BepInEx ``plugins/`` directory."""

    def __init__(self, plugins_dir):
        if not plugins_dir:
            raise ValueError("plugins_dir is required (could not be resolved from config)")
        self.plugins_dir = plugins_dir

    # --- plumbing -------------------------------------------------------------

    def path(self, name):
        return os.path.join(self.plugins_dir, name)

    def exists(self, name):
        return os.path.exists(self.path(name))

    def _dir_owner(self):
        """``(uid, gid)`` to chown things we create to, or None when there is nothing to do.

        Only ever non-None while running as root against a directory somebody else owns --
        i.e. exactly the ``docker compose exec`` (defaults to root) case that left
        root-owned protocol files the plugin, running as botuser, could not write.
        """
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None or geteuid() != 0:  # pragma: no cover - non-POSIX fallback
            return None
        try:
            st = os.stat(self.plugins_dir)
        except OSError:
            return None
        if st.st_uid == 0:
            return None
        return st.st_uid, st.st_gid

    @staticmethod
    def _chown_quietly(path, uid, gid):
        """A failed chown must never turn a successful write into an exception."""
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            print("[Protocol] WARNING: could not chown {} to {}:{}: {}".format(
                path, uid, gid, exc))

    @contextmanager
    def lock(self):
        """Exclusive advisory lock shared by every control-plane writer process."""
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        os.makedirs(self.plugins_dir, exist_ok=True)
        fd = os.open(self.path(LOCK_FILENAME), os.O_CREAT | os.O_RDWR, 0o666)
        # Not hypothetical, and one layer deeper than the reported bug: the lock file is
        # created 0o666 *before umask*, so a root-created one lands as 0644 root:root under
        # the default umask -- after which every later botuser writer fails its
        # os.open(..., O_RDWR) with EACCES and no control-plane write works at all.
        owner = self._dir_owner()
        if owner is not None:
            self._chown_quietly(self.path(LOCK_FILENAME), owner[0], owner[1])
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _atomic_write(self, name, content):
        os.makedirs(self.plugins_dir, exist_ok=True)
        target = self.path(name)
        tmp = "{}.tmp.{}".format(target, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Chown the TEMP file, so the atomic rename publishes an already-correctly-owned
        # file: there is never a window in which the live file is root-owned.
        owner = self._dir_owner()
        if owner is not None:
            self._chown_quietly(tmp, owner[0], owner[1])
        os.replace(tmp, target)

    def _check_writable(self, name):
        if name in RESET_ONLY:
            raise ProtocolOwnershipError(
                "'{}' is plugin-owned runtime state: {} Use reset_rotation_state()/"
                "clear_shuffle_order() instead of writing content.".format(name, RESET_ONLY[name])
            )
        if name in READ_ONLY:
            raise ProtocolOwnershipError(
                "'{}' is produced by the plugin and is read-only for the control "
                "plane.".format(name)
            )

    # --- reads ----------------------------------------------------------------

    def read_text(self, name, default=None):
        try:
            with open(self.path(name), "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return default

    def read_flag(self, name, default=False):
        """Mirror of the plugin's ``FileFlag``: present AND content == 'true'."""
        raw = self.read_text(name)
        if raw is None:
            return default
        return raw.strip().lower() == "true"

    def read_int(self, name, default=None):
        raw = self.read_text(name)
        if raw is None:
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    def read_lines(self, name):
        raw = self.read_text(name)
        if raw is None:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def read_static_track_lines(self):
        """The raw, trimmed, non-blank, non-``#`` lines of ``tracks_to_rotate.txt`` --
        byte-identical to what the plugin's own ``ReadStaticTracks`` (Plugin.Rotation.cs)
        returns. Used by ``dashboard.control.shuffle_order`` to recompute the same
        content signature the plugin guards ``shuffle_order.txt`` with; deliberately NOT
        the parsed ``{track, environment, mode}`` dicts from ``read_rotation_tracks()``
        below, whose reconstruction (rejoined with a plain ``,``) loses whatever exact
        whitespace the original line had and would make the signature never match.
        """
        return [line for line in self.read_lines("tracks_to_rotate.txt") if not line.startswith("#")]

    def read_rotation_tracks(self):
        """Parse ``tracks_to_rotate.txt`` into ``[{track, environment, mode}]``.

        Comment lines (``#``) are the header the writer emits and are skipped, so the
        list index equals the plugin's rotation cursor index into the static file.
        """
        tracks = []
        for line in self.read_lines("tracks_to_rotate.txt"):
            if line.startswith("#"):
                continue
            track, environment, mode = parse_track_line(line)
            tracks.append({
                "track": track,
                "environment": environment,
                "mode": mode,
            })
        return tracks

    # --- writes ---------------------------------------------------------------

    def write_text(self, name, value):
        """Atomically write a control-plane-owned protocol file."""
        self._check_writable(name)
        with self.lock():
            self._atomic_write(name, value)

    def write_flag(self, name, value):
        self.write_text(name, _bool_text(value))

    def delete(self, name):
        """Delete a control-plane-owned protocol file (absent = silent no-op)."""
        self._check_writable(name)
        with self.lock():
            try:
                os.remove(self.path(name))
                return True
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return False
                raise

    # --- plugin-owned state: reset only ---------------------------------------

    def reset_rotation_state(self):
        """Reset the plugin's rotation cursor to 0. The ONLY sanctioned write to
        ``rotation_state.txt`` from the control plane."""
        with self.lock():
            self._atomic_write("rotation_state.txt", "0")

    def read_workshop_download_result(self):
        """Parsed ``workshop_download_result.txt``, or None when absent/half-written."""
        return parse_workshop_download_result(self.read_text("workshop_download_result.txt"))

    def consume_workshop_download_result(self):
        """Read the result file and delete it, returning the parsed record (or None).

        The ONLY sanctioned mutation of ``workshop_download_result.txt`` from the control
        plane (it is plugin-produced, hence in READ_ONLY). Consuming rather than merely
        reading is what keeps it a one-shot signal: leaving it behind would make the next
        download request read a stale outcome. A file present but not yet parseable is
        left alone -- deleting a half-written line would destroy the real result.
        """
        with self.lock():
            record = self.read_workshop_download_result()
            if record is None:
                return None
            try:
                os.remove(self.path("workshop_download_result.txt"))
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            return record

    def clear_shuffle_order(self):
        """Delete the plugin's persisted shuffle deal so it re-deals from scratch. The
        ONLY sanctioned mutation of ``shuffle_order.txt`` from the control plane."""
        with self.lock():
            try:
                os.remove(self.path("shuffle_order.txt"))
                return True
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return False
                raise

    # --- typed setters (what the orchestrator's main() used to write inline) ---

    def set_lobby_name(self, name):
        self.write_text("lobby_name.txt", str(name))

    def set_rotation_interval(self, seconds):
        self.write_text("rotation_interval.txt", str(int(seconds)))

    def set_room_private(self, private):
        self.write_flag("room_private.txt", private)

    def set_auto_start(self, enabled):
        self.write_flag("auto_start.txt", enabled)

    def set_shuffle_mode(self, enabled):
        self.write_flag("shuffle_mode.txt", enabled)

    def set_democracy_mode(self, enabled):
        self.write_flag("democracy_mode.txt", enabled)

    def set_max_players(self, count):
        self.write_text("max_players.txt", str(int(count)))

    def set_playlist_name(self, name):
        self.write_text("playlist_name.txt", str(name))

    def set_log_dir(self, log_dir):
        self.write_text("log_dir.txt", str(log_dir))

    def set_available_playlists(self, names):
        self.write_text("available_playlists.txt", "".join("{}\n".format(n) for n in names))

    def set_rotation_paused(self, paused):
        self.write_flag("rotation_paused.txt", paused)

    def set_rotation_engaged(self, engaged):
        self.write_flag("rotation_engaged.txt", engaged)

    def set_maintenance(self, active):
        """Schedule (or cancel) a maintenance shutdown.

        The plugin's external-maintenance check is presence-based
        (``RunServerMaintenanceTick``: file exists -> schedule + announce; file gone
        while active -> ``CancelMaintenance()`` + announce), which is exactly why the
        cancel path deletes rather than writing ``false``.
        """
        if active:
            self.write_text("maintenance_active.txt", "true")
            return True
        return self.delete("maintenance_active.txt")

    def set_override_game_mode(self, mode):
        """Force a game mode, or clear the override when ``mode`` is falsy (the plugin
        treats an absent/empty file as 'no override')."""
        if mode:
            self.write_text("override_game_mode.txt", str(mode))
            return True
        return self.delete("override_game_mode.txt")

    def request_workshop_download(self, published_id):
        """Ask the plugin to download one workshop item in-game (no restart).

        One-shot, like ``trigger_skip_now``: the plugin deletes the file the instant it
        starts processing it, so there is no "cancel" side and no second copy of "is a
        download pending" to keep in sync -- the request file's existence *is* that state
        until the plugin takes it. Content is a bare decimal id, nothing else; the plugin
        answers ``bad_id`` for anything it cannot parse, so validation lives in exactly
        one place rather than being duplicated here -- the one exception is an *empty*
        id, refused outright because an empty request file is not a protocol message at
        all: the plugin would have no id to echo back, and its result line would be
        unparseable, so the requester would sit through its whole timeout instead of
        being told anything.
        """
        value = str(published_id).strip()
        if not value:
            raise ValueError("workshop_download_request.txt needs a published file id")
        self.write_text("workshop_download_request.txt", value)

    def request_workshop_unsubscribe(self, ids):
        """Ask the plugin to ``SteamUGC.UnsubscribeItem`` each of ``ids``.

        Written *after* the item has been quarantined, never before: Steam may delete an
        unsubscribed item's content directory, which would race the ``shutil.move`` in
        ``quarantine_item()`` and lose the very files the operator needs to diagnose the
        rejection (workshop-ingest-hardening.md, decision 9). Unsubscribing at all is what
        stops Steam quietly re-downloading a rejected item and keeps it out of
        ``SteamUGC.GetSubscribedItems``, i.e. out of the next availability sweep and so out
        of the resolver's reach.

        Non-numeric ids are refused here with ``ValueError`` rather than passed through:
        unlike the download request there is no result file to carry a ``bad_id`` back, so
        a junk id would just be dropped silently by the plugin.
        """
        values = []
        for published_id in (ids or []):
            value = str(published_id).strip()
            if not value.isdigit():
                raise ValueError(
                    "workshop_unsubscribe_request.txt takes decimal published file ids, "
                    "got {!r}".format(published_id))
            values.append(value)
        if not values:
            raise ValueError("workshop_unsubscribe_request.txt needs at least one id")
        if len(values) > MAX_UNSUBSCRIBE_IDS:
            raise ValueError("at most {} ids per unsubscribe request, got {}".format(
                MAX_UNSUBSCRIBE_IDS, len(values)))
        self.write_text(WORKSHOP_UNSUBSCRIBE_REQUEST_FILE,
                        "".join("{}\n".format(v) for v in values))

    def claim_workshop_downloads(self, ids):
        """Mark ``ids`` as driven by a blocking CLI run, so the auto-ingest leaves them.

        Re-calling this refreshes the file's mtime, which is how a legitimately long run
        (a slow download plus a multi-minute sweep wait) avoids being treated as stale.
        """
        self.write_text(WORKSHOP_CLAIM_FILE,
                        "".join("{}\n".format(str(i).strip()) for i in ids))

    def read_workshop_download_claim(self):
        """``(ids, mtime)`` of the claim file; ``([], None)`` when there is none."""
        try:
            mtime = os.path.getmtime(self.path(WORKSHOP_CLAIM_FILE))
        except OSError:
            return [], None
        return self.read_lines(WORKSHOP_CLAIM_FILE), mtime

    def release_workshop_downloads(self):
        """Drop the claim (always from a ``finally``: an unreleased claim would make the
        auto-ingest ignore results until the staleness bound expires)."""
        return self.delete(WORKSHOP_CLAIM_FILE)

    def trigger_skip_now(self):
        """One-shot immediate-rotation request (bot-dashboard.md skip-now fix).

        Unlike ``set_maintenance``, there is no "cancel" side: the plugin polls for this
        file's presence inside ``HandleGameRoom`` and deletes it itself the moment it is
        consumed (same presence-only convention as ``maintenance_active.txt``, but
        one-shot rather than toggled), so the control plane's only sanctioned action is
        to create it. Content is ignored by the reader; "true" matches the flag-file
        wording used elsewhere for a human reading the directory.
        """
        self.write_text("skip_now.txt", "true")

    def enqueue_say(self, message):
        """Enqueue an operator chat message for the plugin to send verbatim, as plain
        text, on its next 1s tick (dashboard-chat-send.md).

        Writes a new sequenced one-shot file ``say_<n>.txt`` where ``n`` is one past the
        highest existing ``say_*.txt`` sequence number (starting at 1). The scan-then-
        write is done under a single acquisition of ``self.lock()`` so two concurrent
        callers (dashboard + orchestrator, or two dashboard requests) can never be
        assigned the same sequence number -- the exact race a single ``say_now.txt``
        flag file would have (a second send within one plugin poll tick silently
        overwriting the first, losing a message). The plugin consumes files in
        ascending numeric order and deletes each one after sending, so this is a
        one-shot request per file, never a toggled/replayed flag (same convention as
        ``skip_now.txt``).

        Raises ``ValueError`` for an empty/whitespace-only message or one over
        ``MAX_SAY_MESSAGE_LENGTH`` characters -- both are rejected here, before a file
        ever reaches the plugin, rather than relying on the plugin to cope with junk.

        Returns the assigned sequence number.
        """
        if message is None:
            raise ValueError("message is required")
        text = message.strip()
        if not text:
            raise ValueError("message must not be empty or whitespace-only")
        if len(text) > MAX_SAY_MESSAGE_LENGTH:
            raise ValueError(
                "message is {} characters, over the {}-character limit".format(
                    len(text), MAX_SAY_MESSAGE_LENGTH
                )
            )
        with self.lock():
            os.makedirs(self.plugins_dir, exist_ok=True)
            seq = self._next_say_sequence()
            self._atomic_write("say_{}.txt".format(seq), text)
        return seq

    def _next_say_sequence(self):
        """One past the highest existing ``say_*.txt`` sequence number, or 1 if none
        exist. Callers must hold ``self.lock()`` -- this only scans, it does not lock
        itself, so the scan and the subsequent write are one atomic unit from the
        perspective of any other writer taking the same lock."""
        highest = 0
        try:
            names = os.listdir(self.plugins_dir)
        except OSError:
            names = []
        for name in names:
            m = SAY_FILE_RE.match(name)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest + 1
