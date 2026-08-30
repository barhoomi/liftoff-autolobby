"""Current-bot-state derivation: JSONL events + protocol files -> one snapshot dict.

Nothing here asks the game or the plugin anything. The two halves of the picture are:

- **Configuration** — read straight from the protocol files, because those files *are*
  the configuration the plugin obeys (interval, lobby name, visibility, auto-start,
  maintenance, active playlist, the static rotation list).
- **Liveness** — folded from the JSONL event stream, which is the only channel the
  plugin has to tell an outside process what it did (rotation, room_entered,
  player_join/leave, disconnect, errors).

Everything is best-effort and explicitly nullable: the dashboard shows "unknown" rather
than a fabricated value when the events that would have told it never happened (e.g. a
bot that has been up for 10 minutes but hasn't rotated yet has no current track).
"""

from datetime import datetime, timezone

from . import events as events_mod
from . import shuffle_order as shuffle_order_mod

# Events that mean "whatever roster we had is gone": the game process restarted, or the
# bot left/lost the room. Without these a disconnect would leave ghosts in the player
# list until someone happened to emit a matching player_leave (which never arrives).
ROSTER_RESET_EVENTS = {"orchestrator_start", "game_start"}

MAX_RECENT_ERRORS = 10


def parse_ts(ts):
    """Parse the schema's ``YYYY-MM-DDTHH:MM:SSZ`` into an aware datetime (or None)."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_seconds(ts, now):
    dt = parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds())


def _player_key(event):
    return event.get("userId") or event.get("player") or ""


def fold_events(event_list, now=None):
    """Fold a chronological event list into the liveness half of the snapshot."""
    now = now or events_mod.utc_now()

    state = {
        "current_track": None,
        "current_environment": None,
        "current_mode": None,
        "rotation_index": None,
        "rotation_started_ts": None,
        "room_entered_ts": None,
        "orchestrator_start_ts": None,
        "game_start_ts": None,
        "game_pid": None,
        "scene": None,
        "last_event_ts": None,
        "last_disconnect": None,
        "players": [],
        "player_count": None,
        "recent_errors": [],
        "event_count": len(event_list),
        "chat_count": 0,
    }

    roster = {}

    for event in event_list:
        name = event.get("event")
        ts = event.get("ts")
        if ts:
            state["last_event_ts"] = ts

        if name in ROSTER_RESET_EVENTS:
            roster = {}
            state["player_count"] = None
            state["current_track"] = None
            state["current_environment"] = None
            state["current_mode"] = None
            state["rotation_index"] = None
            state["rotation_started_ts"] = None
            state["room_entered_ts"] = None
            if name == "orchestrator_start":
                state["orchestrator_start_ts"] = ts
            else:
                state["game_start_ts"] = ts
                state["game_pid"] = event.get("pid")

        elif name == "rotation":
            state["current_track"] = event.get("track")
            state["current_environment"] = event.get("env")
            state["current_mode"] = event.get("mode")
            state["rotation_index"] = event.get("index")
            # The rotation timer restarts when the bot commits to the next track, so this
            # is also the freshest "the countdown began now" signal available.
            state["rotation_started_ts"] = ts

        elif name == "room_entered":
            state["room_entered_ts"] = ts
            # A genuinely fresh room (not an in-place settings update) resets the timer.
            if event.get("fresh"):
                state["rotation_started_ts"] = ts

        elif name == "scene_change":
            state["scene"] = event.get("to") or event.get("scene")

        elif name == "player_join":
            key = _player_key(event)
            if key:
                roster[key] = {"player": event.get("player"), "userId": event.get("userId"),
                               "since": ts}
            if isinstance(event.get("count"), int):
                state["player_count"] = event["count"]

        elif name == "player_leave":
            roster.pop(_player_key(event), None)
            if isinstance(event.get("count"), int):
                state["player_count"] = event["count"]

        elif name == "disconnect":
            state["last_disconnect"] = {"ts": ts, "cause": event.get("cause"),
                                        "elapsed_s": event.get("elapsed_s")}
            roster = {}
            state["player_count"] = None

        elif name == "chat":
            state["chat_count"] += 1

        elif name == "error":
            state["recent_errors"].append({
                "ts": ts, "source": event.get("source"),
                "message": event.get("message"), "context": event.get("context"),
            })

    state["players"] = list(roster.values())
    state["recent_errors"] = state["recent_errors"][-MAX_RECENT_ERRORS:]
    state["room_age_s"] = _age_seconds(state["room_entered_ts"], now)
    state["uptime_s"] = _age_seconds(state["orchestrator_start_ts"] or state["game_start_ts"], now)
    state["last_event_age_s"] = _age_seconds(state["last_event_ts"], now)
    return state


def read_config_state(protocol):
    """The configuration half: the protocol files exactly as the plugin reads them."""
    tracks = protocol.read_rotation_tracks()
    shuffle_mode = protocol.read_flag("shuffle_mode.txt")

    # bug-comma-in-track-name.md, Bug 3 (operator live report): with shuffle mode on,
    # the ACTUAL play order is a permutation of tracks_to_rotate.txt held in the
    # plugin-owned shuffle_order.txt (see dashboard.control.shuffle_order's docstring
    # for the ownership/self-healing rules this must respect -- read-only, never
    # written here). When a trustworthy permutation is on disk, present `tracks` in
    # that order and set `shuffled` so the UI can label it; otherwise (shuffle off, or
    # the deal is missing/stale/invalid) fall back to tracks_to_rotate.txt's own
    # definition order with `shuffled: False` -- exactly what the plugin itself falls
    # back to before it has (re)dealt.
    shuffled = False
    if shuffle_mode:
        static_lines = protocol.read_static_track_lines()
        if len(static_lines) == len(tracks):
            order, shuffled = shuffle_order_mod.read_active_order(protocol, static_lines)
            if shuffled:
                tracks = [tracks[i] for i in order]

    return {
        "plugins_dir": protocol.plugins_dir,
        "lobby_name": protocol.read_text("lobby_name.txt"),
        "playlist": protocol.read_text("playlist_name.txt"),
        "available_playlists": protocol.read_lines("available_playlists.txt"),
        "rotation_interval_s": protocol.read_int("rotation_interval.txt"),
        "room_private": protocol.read_flag("room_private.txt", None),
        "auto_start": protocol.read_flag("auto_start.txt"),
        "shuffle_mode": shuffle_mode,
        # Whether `tracks` below is actually presented in the plugin's shuffled walk
        # order (as opposed to shuffle_mode being on but no trustworthy deal being
        # available yet to reflect it) -- see the shuffled-order note above.
        "shuffled": shuffled,
        "democracy_mode": protocol.read_flag("democracy_mode.txt"),
        "max_players": protocol.read_int("max_players.txt"),
        "override_game_mode": protocol.read_text("override_game_mode.txt"),
        "maintenance_active": protocol.exists("maintenance_active.txt"),
        # Absent rotation_engaged.txt means engaged (the plugin's documented default);
        # absent rotation_paused.txt means not paused.
        "rotation_engaged": protocol.read_flag("rotation_engaged.txt", True),
        "rotation_paused": protocol.read_flag("rotation_paused.txt", False),
        "rotation_cursor": protocol.read_int("rotation_state.txt"),
        "track_count": len(tracks),
        # In definition order when `shuffled` is False, in the plugin's actual walk
        # order when it is True -- either way, index i here is the i-th track that will
        # actually play (which is also what makes the next_track derivation below a
        # single formula for both modes now).
        "tracks": tracks,
    }


def build_snapshot(protocol, log_dir, limit=500, now=None, event_list=None):
    """The dashboard's state panel: configuration + liveness + the derived countdown."""
    now = now or events_mod.utc_now()
    if event_list is None:
        event_list = events_mod.read_recent(log_dir, limit=limit, now=now)

    config_state = read_config_state(protocol)
    live = fold_events(event_list, now=now)

    interval = config_state["rotation_interval_s"]
    elapsed = _age_seconds(live["rotation_started_ts"], now)
    remaining = None
    if interval is not None and elapsed is not None:
        remaining = max(0.0, interval - elapsed)
    if config_state["rotation_paused"] or not config_state["rotation_engaged"]:
        # The plugin does not run the countdown down in these states, so a number here
        # would be a lie that keeps ticking.
        remaining = None

    next_track = None
    tracks = config_state["tracks"]
    cursor = config_state["rotation_cursor"]
    if tracks and isinstance(cursor, int):
        # rotation_state.txt is the cursor of the NEXT track to be served, as a position
        # in the WALK order -- which `tracks` above now already IS, in both modes: file
        # (definition) order when `shuffled` is False, the plugin's persisted permutation
        # order when True. So this one formula is correct either way; when shuffle is on
        # but no trustworthy shuffle_order.txt is available yet (`shuffled` is False for
        # that reason too), `tracks` is still file order and the cursor does NOT index
        # into it correctly, so next_track is deliberately withheld rather than guessed
        # (AGENTS.md rule 4 -- re-deriving the plugin's own derived state here would be
        # exactly the stale-copy bug shape that bit the original shuffle bug).
        if not config_state["shuffle_mode"] or config_state["shuffled"]:
            next_track = tracks[cursor % len(tracks)]

    return {
        "generated_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "log_dir": log_dir,
        "config": config_state,
        "live": live,
        "rotation": {
            "current_track": live["current_track"],
            "current_environment": live["current_environment"],
            "current_mode": live["current_mode"],
            "index": live["rotation_index"],
            "started_ts": live["rotation_started_ts"],
            "elapsed_s": elapsed,
            "interval_s": interval,
            "remaining_s": remaining,
            "next_track": next_track,
            "paused": config_state["rotation_paused"],
            "engaged": config_state["rotation_engaged"],
        },
    }
