"""First-run track bootstrap — breaking the fresh-install chicken-and-egg deadlock.

See ``docs/features/doing/fresh-install-track-bootstrap-deadlock.md`` (option 3).

The problem in one paragraph: official Liftoff tracks are baked into Unity asset
bundles, not shipped as loose ``*.track`` files, so ``gather_tracks.py`` can only learn
they exist by reconciling against ``ui_tracks_dump.json`` — a file the *plugin* writes
while it drives the room-creation settings popup. On a genuinely fresh install
``master_tracks_list.json`` is absent/empty, every playlist resolves to 0 tracks, and
the historical orchestrator either exited or wrote an empty rotation and never looked
again — so the dump was captured (see below) but nothing ever consumed it.

What this module adds is only the *consuming* half, and it is deliberately a state
machine rather than a blocking wait so the monitor loop keeps servicing everything else
(playlist changes, game-process watchdog) while a first boot completes:

    armed (master list has no resolvable tracks)
      -> note_game_started()            the watchdog launched Liftoff
      -> poll() until track_dump_ready() the plugin's Environment x GameMode sweep landed
      -> gather_tracks_and_races()      reconcile the dump into master_tracks_list.json
      -> resolve_and_write_playlist()   the same resolver startup uses; no parallel path
      -> completed                      the plugin picks the rotation up on its next tick

No plugin change is involved and none is needed. ``ConfigureAndCreateRoom``
(``plugin/Plugin.RoomSetup.cs``) runs the availability sweep *before* delegating to
``ApplyRoomSettingsPopup``, which is where the empty-``targetTrackName`` cancel now lives
— so an empty rotation no longer suppresses the dump, it only cancels the room creation
that follows it. Confirmed game-side by decompiling
``Liftoff.Multiplayer.GameSetup.ContentSettingsPanel`` (AGENTS.md rule 1): the game-mode
dropdown is filled from a hardcoded 4-mode list, the environment dropdown from
``EnvironmentContainer.use.GetAllVisible(...)``, and both ``OnEnvironmentSelected`` and
``OnGameModeSelected`` call ``FillContentSelection()`` synchronously — i.e. the sweep's
dropdown walk reads the game's own content catalogue and depends on none of the
orchestrator's files.

Single source of truth (AGENTS.md rule 4): this module introduces **no** new track file.
It waits on plugin-produced data and then re-runs the two functions that already own
"what tracks exist" and "what does this playlist resolve to".
"""

import json
import os
import time

from trackcheck.playlist_match import RESOLVABLE_CATEGORIES

from . import paths  # noqa: F401  (import performs the orchestrator sys.path bootstrap)

# Wall-clock budget, from the moment the game process is launched, for the plugin to
# reach the multiplayer settings popup and finish its Environment x GameMode sweep.
# Generous on purpose: it has to cover Steam/Photon sign-in, the menu walk, and the
# sweep itself (~1 tick per environment/mode pair, one second per tick).
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0

TIMEOUT_ENV_VAR = "FPV_BOOTSTRAP_TIMEOUT"

# The two files the plugin's sweep writes, in the order WriteLegacyUiTracksDump /
# WriteTrackModeAvailabilityDump are called. Requiring the *second* one to be readable
# is what makes "the first one is complete" safe to assume: neither is written
# atomically, so existence alone is not enough (see track_dump_ready).
LEGACY_DUMP_FILE = "ui_tracks_dump.json"      # consumed by gather_tracks.py
AVAILABILITY_DUMP_FILE = "track_mode_availability.json"  # consumed by playlists.py

# Value of the `kind` field on the `decision` events this module emits.
DECISION_KIND = "track_bootstrap"


def _load_json(path):
    """Parse a JSON file, returning None if it is absent, unreadable or half-written."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def count_master_tracks(master_data):
    """Number of *resolvable* track names in a parsed ``master_tracks_list.json``.

    Counts only ``RESOLVABLE_CATEGORIES`` (official + workshop) because those are the
    only categories a playlist can ever resolve from — a master list holding nothing but
    ``local`` tracks resolves to an empty rotation just as surely as an empty one does.
    Tolerates both catalogue shapes in use: ``{track: [races]}`` and a bare ``[track]``.
    """
    if not isinstance(master_data, dict):
        return 0
    total = 0
    for categories in master_data.values():
        if not isinstance(categories, dict):
            continue
        for category in RESOLVABLE_CATEGORIES:
            entry = categories.get(category)
            if isinstance(entry, (dict, list)):
                total += len(entry)
    return total


def master_list_has_tracks(master_list_path):
    """True when ``master_tracks_list.json`` exists, parses, and holds >=1 track.

    This is the whole "is this a fresh install?" test. A missing file, an unparseable
    one, and the ``{}`` that ``gather_tracks_and_races()`` writes when it finds nothing
    are all the same situation and all answer False.
    """
    return count_master_tracks(_load_json(master_list_path)) > 0


def dump_environment_count(plugins_dir):
    """Number of environments in the plugin's legacy dump, or 0 if it isn't readable."""
    data = _load_json(os.path.join(plugins_dir, LEGACY_DUMP_FILE))
    return len(data) if isinstance(data, dict) else 0


def track_dump_ready(plugins_dir):
    """True once the plugin's sweep has landed a *complete, usable* pair of dumps.

    Three conditions, each earning its place:

    - both files parse as JSON objects — the plugin writes them with
      ``File.WriteAllLines`` (not atomically), so a partially written file is a real
      possibility and shows up here as a parse failure, i.e. "not ready yet";
    - ``track_mode_availability.json`` is written *after* ``ui_tracks_dump.json``, so
      requiring it to parse means the file gather_tracks actually consumes is complete;
    - at least one environment lists at least one track — a dump where every environment
      came back empty means the game has no content loaded, and reconciling it would
      just write another empty master list.
    """
    availability = _load_json(os.path.join(plugins_dir, AVAILABILITY_DUMP_FILE))
    if not isinstance(availability, dict):
        return False
    legacy = _load_json(os.path.join(plugins_dir, LEGACY_DUMP_FILE))
    if not isinstance(legacy, dict):
        return False
    return any(isinstance(tracks, list) and tracks for tracks in legacy.values())


def bootstrap_timeout(env=None):
    """Timeout in seconds: ``FPV_BOOTSTRAP_TIMEOUT`` if set and valid, else the default.

    ``<= 0`` disables the bootstrap entirely (the caller then keeps the historical
    fail-fast behaviour) — an escape hatch for an operator who would rather see the
    orchestrator exit than have it wait on a game that may never reach the menu.
    """
    env = os.environ if env is None else env
    raw = env.get(TIMEOUT_ENV_VAR)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"[Bootstrap] WARNING: ignoring unparseable {TIMEOUT_ENV_VAR}={raw!r}; "
              f"using {DEFAULT_TIMEOUT_SECONDS}s.")
        return DEFAULT_TIMEOUT_SECONDS


def _default_gather():
    from gather_tracks import gather_tracks_and_races
    return gather_tracks_and_races()


def _default_resolve(playlist_name, shuffle, tracks_file, logger=None):
    from .playlists import resolve_and_write_playlist
    return resolve_and_write_playlist(playlist_name, shuffle, tracks_file, logger=logger)


class TrackBootstrap:
    """One first-run bootstrap attempt, driven from the orchestrator's monitor loop.

    Construct it only when a bootstrap is actually needed (``master_list_has_tracks()``
    is False); constructing it records the decision on the event log. Then call
    ``note_game_started()`` when the watchdog launches Liftoff and ``poll()`` freely from
    the loop — ``poll`` rate-limits itself to ``poll_interval`` so calling it every tick
    costs nothing.

    ``gather`` / ``resolve`` are injectable purely so the whole state machine is unit
    testable without a game (or even a real master list); the defaults are the same
    ``gather_tracks_and_races`` / ``resolve_and_write_playlist`` the orchestrator's own
    startup path calls, so there is exactly one implementation of each step.
    """

    IDLE = "idle"            # armed, waiting for the game process to be launched
    WAITING = "waiting"      # game up, waiting for the plugin's dump to land
    COMPLETED = "completed"  # master list regenerated and playlist re-resolved
    TIMEOUT = "timeout"      # dump never landed within the budget
    FAILED = "failed"        # dump landed but gather/resolve raised

    def __init__(self, plugins_dir, playlist_name, tracks_file, shuffle=False,
                 logger=None, timeout=None, poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                 clock=time.monotonic, gather=None, resolve=None):
        self.plugins_dir = plugins_dir
        self.playlist_name = playlist_name
        self.tracks_file = tracks_file
        self.shuffle = shuffle
        self.logger = logger
        self.timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
        self.poll_interval = poll_interval
        self._clock = clock
        self._gather = gather or _default_gather
        self._resolve = resolve or _default_resolve

        self.state = self.IDLE
        self.started_at = None
        self.elapsed = None
        self.resolved_count = None
        self.environment_count = None
        self._last_poll = None

        self._emit_decision(
            f"master_tracks_list.json has no resolvable tracks — first-run bootstrap armed; "
            f"will wait up to {self.timeout:.0f}s for the plugin's track dump, then "
            f"regenerate the master list and re-resolve playlist '{playlist_name}'")

    # --- state ---------------------------------------------------------------

    @property
    def active(self):
        """True while the bootstrap still has work to do (idle or waiting)."""
        return self.state in (self.IDLE, self.WAITING)

    @property
    def deadline(self):
        """Monotonic instant the wait gives up at, or None before the game is launched."""
        if self.started_at is None:
            return None
        return self.started_at + self.timeout

    # --- driving -------------------------------------------------------------

    def note_game_started(self):
        """Arm the timeout: the game process the dump depends on is now running."""
        if self.state != self.IDLE:
            return self.state
        self.started_at = self._clock()
        self.state = self.WAITING
        print(f"[Bootstrap] Waiting for the plugin's track dump in {self.plugins_dir} "
              f"(up to {self.timeout:.0f}s)...")
        return self.state

    def poll(self, force=False):
        """Advance the state machine; cheap and safe to call on every loop tick."""
        if not self.active or self.state == self.IDLE:
            return self.state

        now = self._clock()
        if not force and self._last_poll is not None and \
                (now - self._last_poll) < self.poll_interval:
            return self.state
        self._last_poll = now

        if track_dump_ready(self.plugins_dir):
            return self._complete(now)

        if now >= self.deadline:
            self.elapsed = now - self.started_at
            self.state = self.TIMEOUT
            message = (f"first-run track bootstrap timed out after {self.elapsed:.0f}s: "
                       f"the plugin never produced a usable {LEGACY_DUMP_FILE} in "
                       f"{self.plugins_dir}. The rotation is still empty; restart the bot "
                       f"once the game reaches the multiplayer menu.")
            print(f"[Bootstrap] ERROR: {message}")
            if self.logger:
                self.logger.error(message, context=DECISION_KIND)
            return self.state

        return self.state

    # --- internals -----------------------------------------------------------

    def _complete(self, now):
        self.elapsed = now - self.started_at
        self.environment_count = dump_environment_count(self.plugins_dir)
        print(f"[Bootstrap] Track dump captured after {self.elapsed:.0f}s "
              f"({self.environment_count} environments) — regenerating the master track "
              f"list and re-resolving playlist '{self.playlist_name}'...")
        try:
            self._gather()
            resolved = self._resolve(self.playlist_name, self.shuffle, self.tracks_file,
                                     logger=self.logger)
        except Exception as exc:
            self.state = self.FAILED
            message = (f"first-run track bootstrap failed while reconciling the plugin's "
                       f"track dump: {exc}")
            print(f"[Bootstrap] ERROR: {message}")
            if self.logger:
                self.logger.error(message, context=DECISION_KIND,
                                  playlist=self.playlist_name)
            return self.state

        self.resolved_count = len(resolved or [])
        self.state = self.COMPLETED
        print(f"[Bootstrap] First-run bootstrap complete: playlist "
              f"'{self.playlist_name}' now resolves to {self.resolved_count} tracks.")
        self._emit_decision(
            f"track dump captured after {self.elapsed:.0f}s ({self.environment_count} "
            f"environments); regenerated master_tracks_list.json and re-resolved playlist "
            f"'{self.playlist_name}' to {self.resolved_count} tracks")
        return self.state

    def _emit_decision(self, detail):
        print(f"[Bootstrap] {detail}")
        if self.logger:
            self.logger.decision(DECISION_KIND, detail)
