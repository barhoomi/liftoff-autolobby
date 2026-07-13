"""Playlist <-> master-list matching semantics, factored out so there is exactly one
implementation shared between the runtime resolver
(`orchestrator/run_headless_lobby.py`'s `resolve_and_write_playlist()`) and the lint
CLI (`trackcheck.lint_playlists`).

This module is a deliberate line-for-line port of the matching logic embedded in
`resolve_and_write_playlist()` (env pattern normalization via ENV_NORMALIZATION,
fnmatch-based track pattern matching, "official"+"workshop" categories only --
"local" tracks are intentionally excluded, they can't be shared to other players).

Why a port instead of an import: `resolve_and_write_playlist()` is not just matching
-- it's interleaved with live-game cross-validation (track_mode_availability.json),
file writes (tracks_to_rotate.txt, rotation_state.txt) and a fallback-recursion path,
all keyed off the *real* repo-root playlists.json/master_tracks_list.json paths
derived from its own `__file__`. Importing it here would mean either running it
against production files as a side effect of running the linter/tests, or reaching
into orchestrator internals to monkeypatch path resolution -- both worse than a
small, obviously-equivalent, independently-tested port. Equivalence is verified
directly rather than assumed: trackcheck/tests/test_playlist_match.py monkeypatches
`run_headless_lobby.__file__` to point at a throwaway fixture repo layout and asserts
this module's resolution matches `resolve_and_write_playlist()`'s output byte-for-
byte on the same fixture data. If `resolve_and_write_playlist()`'s matching logic
ever changes, that test is the tripwire -- update both together.
"""

import fnmatch

# Maps any variant spelling to the canonical display name used as master list keys.
# Kept in sync with resolve_and_write_playlist()'s ENV_NORMALIZATION in
# orchestrator/run_headless_lobby.py -- see this module's docstring for why it's a
# port rather than an import, and test_playlist_match.py for the parity test.
ENV_NORMALIZATION = {
    "thedrawingboard": "The Drawing Board",
    "thedrawingboardcyber": "The Drawing Board",
    "the drawing board": "The Drawing Board",
    "thegreen": "The Green",
    "the green": "The Green",
    "hannover": "Hannover",
    "hall26": "Hall 26",
    "hall 26": "Hall 26",
    "autumn fields": "Autumn Fields",
    "autumnfields": "Autumn Fields",
    "bando city": "Bando City",
    "bandocity": "Bando City",
    "hangar c03": "Hangar C03",
    "hangarc03": "Hangar C03",
    "liftoff arena": "Liftoff Arena",
    "liftoffarena": "Liftoff Arena",
    "pine valley": "Pine Valley",
    "pinevalley": "Pine Valley",
    "straw bale": "Straw Bale",
    "strawbale": "Straw Bale",
    "minus two": "Minus Two",
    "minustwo": "Minus Two",
    "dubai legends": "Dubai Legends",
    "dubailegends": "Dubai Legends",
    "paris drone festival": "Paris Drone Festival",
    "parisdronefestival": "Paris Drone Festival",
    "the pit": "The Pit",
    "thepit": "The Pit",
    "bardwell's yard": "Bardwell's Yard",
    "bardwellsyard": "Bardwell's Yard",
    "russian woodpecker": "The Woodpecker",
    "russianwoodpecker": "The Woodpecker",
    "the woodpecker": "The Woodpecker",
    "thewoodpecker": "The Woodpecker",
    "short circuit": "Short Circuit",
    "shortcircuit": "Short Circuit",
    "marina bay": "Marina Bay",
    "marinabay": "Marina Bay",
    "surtur": "Surtur",
    "permafrost": "Permafrost",
    "rustline": "Rustline",
    "azure district": "Azure District",
    "azuredistrict": "Azure District",
    "melon pan park": "Melon Pan Park",
    "melonpanpark": "Melon Pan Park"
}

# The categories a playlist entry is allowed to resolve tracks from. "local" is
# excluded on purpose -- see orchestrator/run_headless_lobby.py's comment at the same
# spot: local tracks can't be shared to other players, so they must never enter a
# rotation the bot offers to a lobby.
RESOLVABLE_CATEGORIES = ("official", "workshop")


def is_match(pattern, value):
    return fnmatch.fnmatch(value.lower().strip(), pattern.lower().strip())


def normalize_playlist_item(item):
    """Turn one raw playlists.json entry (a bare string, or a dict with
    environment/track/mode keys) into (env_pattern, track_pattern, game_mode).
    Returns None for anything that's neither (resolve_and_write_playlist() silently
    `continue`s on this; callers here should treat None as "not a valid entry shape"
    -- see lint_playlists.py's INVALID_ENTRY_SHAPE check).
    """
    if isinstance(item, str):
        return "*", item, "Infinite Race"
    if isinstance(item, dict):
        return item.get("environment", "*"), item.get("track", "*"), item.get("mode", "Infinite Race")
    return None


def resolve_playlist_item(env_pattern, track_pattern, master_data):
    """Resolve a single (env_pattern, track_pattern) playlist entry against
    master_data (the parsed master_tracks_list.json structure: {env: {category:
    {track_name: [race_names]}}}). Returns a list of (track_name, env_key) tuples,
    matching resolve_and_write_playlist()'s own matching loop exactly (env pattern
    normalization, fnmatch track matching, official+workshop categories only).
    """
    matches = []
    target_env_key = ENV_NORMALIZATION.get(env_pattern.lower().strip(), env_pattern)

    for env_key in master_data:
        if env_pattern != "*" and target_env_key.lower().strip() != env_key.lower().strip():
            continue

        env_entry = master_data[env_key]
        if not isinstance(env_entry, dict):
            continue

        for category in RESOLVABLE_CATEGORIES:
            if category not in env_entry:
                continue
            for track_name in env_entry[category]:
                if is_match(track_pattern, track_name):
                    matches.append((track_name, env_key))

    return matches


def resolve_playlist(playlist_items, master_data):
    """Resolve every entry in a playlist (the list value from playlists.json) against
    master_data. Returns (resolved, per_entry) where `resolved` is the deduplicated
    list of (track_name, env_key, game_mode) tuples in first-seen order (matching
    resolve_and_write_playlist()'s `if track_entry not in resolved_tracks` dedup), and
    `per_entry` is a parallel list of dicts describing each playlist entry's own
    match count (for the linter's per-entry diagnostics).
    """
    resolved = []
    per_entry = []

    for item in playlist_items:
        normalized = normalize_playlist_item(item)
        if normalized is None:
            per_entry.append({"item": item, "valid_shape": False, "env_pattern": None,
                               "track_pattern": None, "mode": None, "match_count": 0})
            continue

        env_pattern, track_pattern, game_mode = normalized
        matches = resolve_playlist_item(env_pattern, track_pattern, master_data)

        for track_name, env_key in matches:
            track_entry = (track_name, env_key, game_mode)
            if track_entry not in resolved:
                resolved.append(track_entry)

        per_entry.append({
            "item": item,
            "valid_shape": True,
            "env_pattern": env_pattern,
            "track_pattern": track_pattern,
            "mode": game_mode,
            "match_count": len(matches),
        })

    return resolved, per_entry
