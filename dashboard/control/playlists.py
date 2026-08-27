"""Playlist resolution — moved out of ``orchestrator/run_headless_lobby.py``.

This is the single implementation of "playlist name -> the static rotation file the
plugin reads" (decision D5). The orchestrator imports it; the dashboard imports it.
Nothing about the *semantics* changed in the move except two deliberate points, both
recorded in ``docs/features/doing/bot-dashboard.md``:

- The glob-matching loop and ``ENV_NORMALIZATION`` are no longer carried here at all;
  they come from ``trackcheck.playlist_match``, which was written as a byte-for-byte
  port of this function's matching (with a parity test). Having two copies of the
  matching rules was the last remaining duplicate of the thing AGENTS.md rule 4 warns
  about; the dicts were verified identical before collapsing them.
- A missing ``master_tracks_list.json`` raises ``MasterTracksMissingError`` instead of
  calling ``sys.exit(1)`` from inside a library. The orchestrator's startup call site
  turns it back into the same message + exit code; a web request handler obviously
  cannot afford a ``SystemExit``.
"""

import json
import os

from trackcheck.playlist_match import ENV_NORMALIZATION, resolve_playlist

from .protocol import ProtocolDir


class PlaylistError(Exception):
    """Base class for playlist-resolution failures."""


class MasterTracksMissingError(PlaylistError):
    """``config/master_tracks_list.json`` is absent (it is generated at runtime by
    ``gather_tracks.py`` from a live game install, so this is a real deployment state)."""


class PlaylistNotFoundError(PlaylistError, ValueError):
    """The requested playlist name is not in playlists.json.

    Subclasses ``ValueError`` because ``run_headless_lobby`` has always caught
    ``ValueError`` at this call site; keeping that true means the orchestrator's error
    path is unchanged by the move.
    """


def default_playlists_path():
    from .paths import playlists_path
    return playlists_path()


def default_master_list_path():
    from .paths import master_tracks_path
    return master_tracks_path()


def load_track_mode_availability(plugins_dir):
    """Ground-truth (environment, game mode) -> [track names] data, produced by the plugin's
    nested dropdown dump (BepInEx/plugins/track_mode_availability.json). Returns None if the
    file is missing/unreadable so callers can fail open rather than block on stale/missing data.
    """
    path = os.path.join(plugins_dir, "track_mode_availability.json")
    if not os.path.exists(path):
        print("[Playlist] WARNING: track_mode_availability.json not found — skipping mode/availability cross-validation.")
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Playlist] WARNING: Failed to load track_mode_availability.json: {e}")
        return None


def cross_validate_tracks(resolved_tracks, availability_data, env_normalization):
    """Drops (track, env, mode) entries that the live game doesn't actually offer. Fails open
    (keeps the entry) whenever there's no ground-truth data to check against, so a missing or
    partial dump can never itself cause a hang.
    """
    if availability_data is None:
        return resolved_tracks, 0, 0

    kept = []
    dropped_missing = 0
    dropped_mode = 0
    for track_name, env_key, game_mode in resolved_tracks:
        env_norm = env_normalization.get(env_key.lower().strip(), env_key)
        env_modes = availability_data.get(env_norm)
        if env_modes is None:
            kept.append((track_name, env_key, game_mode))
            continue

        mode_tracks = env_modes.get(game_mode)
        if mode_tracks is None:
            kept.append((track_name, env_key, game_mode))
            continue

        track_norm = track_name.lower().strip()
        if any(t.lower().strip() == track_norm for t in mode_tracks):
            kept.append((track_name, env_key, game_mode))
            continue

        in_any_mode = any(
            track_norm in (t.lower().strip() for t in tlist)
            for tlist in env_modes.values()
        )
        if in_any_mode:
            dropped_mode += 1
            print(f"[Playlist] DROP '{track_name}' ({env_key}, {game_mode}): mode_unsupported")
        else:
            dropped_missing += 1
            print(f"[Playlist] DROP '{track_name}' ({env_key}, {game_mode}): not_installed_or_not_shared")

    return kept, dropped_missing, dropped_mode


def round_robin_shuffle_by_environment(resolved_tracks):
    """Shuffles each environment's tracks among themselves, shuffles the order environments
    are visited in, then interleaves round-robin across environments. A plain flat shuffle
    can (and does, by chance) land two tracks from the same environment back-to-back — the
    environment is the dominant visual cue players notice, so that reads as "stale"/repetitive
    even though no individual track repeated. Round-robin guarantees same-environment picks
    are spread apart (until an environment runs out of tracks) while staying genuinely random.

    NOT called by resolve_and_write_playlist() as of
    docs/features/doing/bug-shuffle-toggle-and-tracks-incompatibility.md (Option 2,
    2026-07-23): tracks_to_rotate.txt is now always written in playlist definition order,
    and the plugin owns 100% of shuffling as a derived overlay (see
    plugin/Plugin.Rotation.cs's GetActiveRotationOrder/RoundRobinShuffleByEnvironment, a
    ported version of this exact algorithm). Kept here -- rather than deleted along with its
    call site -- because it is pure, well-tested, self-contained logic with no state to go
    stale; if it never regains a caller, it and its dedicated tests are safe to delete
    together.
    """
    import random

    groups = {}
    env_order = []
    for track in resolved_tracks:
        env = track[1]
        if env not in groups:
            groups[env] = []
            env_order.append(env)
        groups[env].append(track)

    for env in env_order:
        random.shuffle(groups[env])
    random.shuffle(env_order)

    result = []
    round_idx = 0
    while len(result) < len(resolved_tracks):
        added_any = False
        for env in env_order:
            if round_idx < len(groups[env]):
                result.append(groups[env][round_idx])
                added_any = True
        if not added_any:
            break
        round_idx += 1
    return result


def resolve_and_write_playlist(playlist_name, shuffle_enabled, output_file, is_fallback=False,
                               logger=None, playlists_path=None, master_list_path=None):
    """Resolve ``playlist_name`` and write the plugin's static rotation file.

    playlists_path/master_list_path default to the real repo files -- they're only ever
    overridden by tests, so master_tracks_list.json's real gitignored/generated-at-runtime
    copy (see AGENTS.md) never has to exist on disk to unit-test this function's
    definition-order behavior in isolation.

    Returns the list of resolved ``(track, environment, mode)`` tuples that were written
    (the orchestrator ignores it; the dashboard reports the count back to the operator).
    """
    if playlists_path is None:
        playlists_path = default_playlists_path()
    if master_list_path is None:
        master_list_path = default_master_list_path()

    # Check playlists file
    if not os.path.exists(playlists_path):
        print(f"ERROR: Playlists file not found at {playlists_path}")
        return None

    with open(playlists_path, "r") as f:
        playlists_data = json.load(f)

    if playlist_name not in playlists_data:
        raise PlaylistNotFoundError(
            f"Playlist '{playlist_name}' not found in playlists.json. "
            f"Available: {list(playlists_data.keys())}")

    playlist_items = playlists_data[playlist_name]

    if not os.path.exists(master_list_path):
        raise MasterTracksMissingError(f"master_tracks_list.json not found at {master_list_path}")

    with open(master_list_path, "r") as f:
        master_data = json.load(f)

    # Matching/dedup/ordering semantics live in trackcheck.playlist_match (one
    # implementation, shared with the lint CLI). resolve_playlist returns the
    # deduplicated (track, env, mode) tuples in first-seen definition order.
    resolved_tracks, _per_entry = resolve_playlist(playlist_items, master_data)

    print(f"[Playlist] Resolved {len(resolved_tracks)} tracks for playlist '{playlist_name}' from master list.")

    # Cross-validate against the plugin's live-game ground truth (which tracks are actually
    # installed/shared, and which game modes each one really supports) before committing to
    # this rotation. Fails open if the dump isn't available yet.
    protocol = ProtocolDir(os.path.dirname(os.path.abspath(output_file)))
    availability_data = load_track_mode_availability(protocol.plugins_dir)
    resolved_tracks, n_missing, n_mode = cross_validate_tracks(resolved_tracks, availability_data,
                                                              ENV_NORMALIZATION)
    print(f"[Playlist] Cross-validation: kept {len(resolved_tracks)} "
          f"(dropped {n_missing} not_installed_or_not_shared, {n_mode} mode_unsupported)")

    if not resolved_tracks:
        if not is_fallback and playlist_name != "all_official_races" and "all_official_races" in playlists_data:
            print(f"[Playlist] CRITICAL: '{playlist_name}' resolved to 0 valid tracks after cross-validation. "
                  f"Falling back to 'all_official_races'.")
            if logger:
                logger.error("playlist resolved to 0 tracks; falling back to all_official_races",
                             context="playlist_resolution", playlist=playlist_name)
            return resolve_and_write_playlist("all_official_races", shuffle_enabled, output_file,
                                              is_fallback=True, logger=logger,
                                              playlists_path=playlists_path, master_list_path=master_list_path)
        else:
            print(f"[Playlist] CRITICAL: '{playlist_name}' resolved to 0 tracks (fallback exhausted or unavailable). "
                  f"Writing empty rotation file — bot may get stuck.")
            if logger:
                logger.error("playlist resolved to 0 tracks; fallback exhausted, writing empty rotation",
                             context="playlist_resolution", playlist=playlist_name)

    # tracks_to_rotate.txt is the STATIC, authoritative playlist definition
    # (docs/features/doing/bug-shuffle-toggle-and-tracks-incompatibility.md, Option 2) --
    # always written here in resolved/definition order, regardless of shuffle_enabled. The
    # orchestrator used to deal a shuffled order into this same file, which was the root
    # cause of that bug: /tracks' PlaylistIndex (a line number in this file) drifted out
    # from under any shuffle, and /shuffle off had no definition order left to restore
    # (the file itself stayed shuffled forever -- "off" only ever flipped a flag). The
    # plugin now owns 100% of shuffling, as a derived, self-regenerating overlay
    # (shuffle_order.txt, plugin-owned, never written here) -- see
    # plugin/Plugin.Rotation.cs's GetActiveRotationOrder/RoundRobinShuffleByEnvironment.
    print("[Playlist] Writing track rotation in playlist definition order "
          f"(shuffle={'on' if shuffle_enabled else 'off'} -- shuffling is applied by the plugin, not here)...")

    lines = ["# Generated from playlist: " + playlist_name + "\n"]
    for track_name, ui_env, game_mode in resolved_tracks:
        lines.append(f"{track_name},{ui_env},{game_mode}\n")
    # Written through ProtocolDir: atomic (the plugin polls this file every second and
    # must never read a half-written rotation) and serialized against the other writer.
    protocol.write_text(os.path.basename(output_file), "".join(lines))
    print(f"[Playlist] Wrote tracks to rotate to: {output_file}")

    # The orchestrator's "track change" signal: the set of tracks in rotation just changed.
    if logger:
        logger.playlist_resolved(playlist_name, len(resolved_tracks), shuffle=shuffle_enabled,
                                 dropped_missing=n_missing, dropped_mode=n_mode,
                                 fallback=is_fallback)

    # Reset rotation state to 0 — the single cursor used for both modes.
    try:
        protocol.reset_rotation_state()
        print(f"[Playlist] Reset rotation state in: {protocol.path('rotation_state.txt')}")
    except Exception as e:
        print(f"[Playlist] WARNING: Failed to reset rotation state file: {e}")

    # A fresh playlist resolution (startup, or an in-session /playlist swap) must also
    # invalidate any persisted shuffle deal. shuffle_order.txt is plugin-owned derived
    # state that self-heals via a content signature (see GetActiveRotationOrder in
    # Plugin.Rotation.cs), but that signature alone can't distinguish "brand new session,
    # same playlist" from "same playlist, still mid-cycle" -- tracks_to_rotate.txt's bytes
    # are identical either way now that this function always writes definition order.
    # Deleting it here (the SAME trigger/call site that already resets rotation_state.txt
    # above -- not a second synchronization channel the orchestrator has to remember to
    # keep in step with the plugin) guarantees a genuinely fresh shuffle-bag deal every
    # session, preserving the promise from docs/features/done/bug-shuffle-state-persists-
    # across-sessions.md ("shuffle order feels stale/precomputed" was that original bug).
    try:
        if protocol.clear_shuffle_order():
            print(f"[Playlist] Cleared stale shuffle order in: {protocol.path('shuffle_order.txt')}")
    except Exception as e:
        print(f"[Playlist] WARNING: Failed to clear shuffle order file: {e}")

    return resolved_tracks
