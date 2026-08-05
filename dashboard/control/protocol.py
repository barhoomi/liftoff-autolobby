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
}

# Plugin-owned runtime state. The plugin writes these; the control plane may ONLY reset
# them (rotation_state.txt -> "0", shuffle_order.txt -> deleted), and only from the one
# call site that already resets both: a fresh playlist resolution.
RESET_ONLY = {
    "rotation_state.txt": "Rotation cursor (plugin-owned; control plane may only reset to 0).",
    "shuffle_order.txt": "Derived shuffle deal (plugin-owned; control plane may only clear).",
}

# Plugin-produced data the control plane reads and must never write.
READ_ONLY = {
    "track_mode_availability.json": "Ground-truth (environment, mode) -> tracks dump from the game UI.",
}

LOCK_FILENAME = ".control.lock"


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

    @contextmanager
    def lock(self):
        """Exclusive advisory lock shared by every control-plane writer process."""
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        os.makedirs(self.plugins_dir, exist_ok=True)
        fd = os.open(self.path(LOCK_FILENAME), os.O_CREAT | os.O_RDWR, 0o666)
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

    def read_rotation_tracks(self):
        """Parse ``tracks_to_rotate.txt`` into ``[{track, environment, mode}]``.

        Comment lines (``#``) are the header the writer emits and are skipped, so the
        list index equals the plugin's rotation cursor index into the static file.
        """
        tracks = []
        for line in self.read_lines("tracks_to_rotate.txt"):
            if line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            tracks.append({
                "track": parts[0] if parts else "",
                "environment": parts[1] if len(parts) > 1 else "",
                "mode": parts[2] if len(parts) > 2 else "",
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
