"""Layer 3 — playlist lint CLI.

    python3 -m trackcheck.lint_playlists [--playlists PATH] [--master PATH]

Validates every playlist in playlists.json against master_tracks_list.json using
exactly the matching semantics `resolve_and_write_playlist()` uses at runtime (see
playlist_match.py's docstring for how that's kept in sync). Flags:

  - EMPTY_RESOLUTION: a playlist resolves to zero tracks
  - ENTRY_NO_MATCHES: one entry within an otherwise-fine playlist matches nothing
    (this is what catches a typo'd track/environment name at commit time instead of
    at 3am when the runtime resolver falls back to all_official_races)
  - UNKNOWN_ENVIRONMENT: an entry's environment pattern isn't "*" and isn't a
    recognized Liftoff environment (reuses trackcheck.parser.ENV_MAPPING)
  - UNKNOWN_MODE: an entry's mode isn't one of the game's known game modes
  - DUPLICATE_ENTRY: the same raw entry appears more than once in one playlist
  - INVALID_ENTRY_SHAPE: an entry is neither a string nor a
    {environment, track, mode} object

Exits nonzero (and prints every finding) if anything above is flagged for any
playlist. Intended to run in `run_tests.sh` and, per the feature doc, to be callable
by the orchestrator at startup for a loud pre-fallback warning.
"""

import argparse
import json
import os
import sys

from trackcheck.parser import ENV_MAPPING
from trackcheck.playlist_match import normalize_playlist_item, resolve_playlist

# Reuse of the plugin's own TrackModeDumpCandidateModes (plugin/Plugin.cs) -- the set
# of game modes the plugin actually knows how to select in the room-settings dropdown.
# Copied rather than imported (Plugin.cs is C#, off-limits to this package, and not
# introspectable from Python); if the plugin's candidate list changes, update this too.
KNOWN_MODES = {"Infinite Race", "Classic Race", "Dropout Race", "Survival"}

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLAYLISTS_PATH = os.path.join(_PROJECT_ROOT, "playlists.json")
DEFAULT_MASTER_PATH = os.path.join(_PROJECT_ROOT, "master_tracks_list.json")


def _is_known_environment_pattern(env_pattern):
    if env_pattern == "*":
        return True
    # Accept either a raw ENV_MAPPING key (any spelling variant) or one of its
    # canonical normalized values -- playlists.json uses canonical spaced names
    # (e.g. "The Drawing Board"), matching ENV_MAPPING's values.
    if env_pattern in ENV_MAPPING:
        return True
    return env_pattern in ENV_MAPPING.values()


def lint_playlists(playlists_data, master_data):
    """Returns a list of finding dicts: {playlist, code, detail}. Empty list means
    clean. Pure function of the two parsed JSON structures -- no file I/O -- so it's
    directly unit-testable against fixtures.
    """
    findings = []

    for playlist_name, items in playlists_data.items():
        if not isinstance(items, list):
            findings.append({
                "playlist": playlist_name, "code": "INVALID_PLAYLIST_SHAPE",
                "detail": f"playlist value must be a list, got {type(items).__name__}",
            })
            continue

        seen_raw = []
        for item in items:
            if item in seen_raw:
                findings.append({
                    "playlist": playlist_name, "code": "DUPLICATE_ENTRY",
                    "detail": f"entry appears more than once: {item!r}",
                })
            else:
                seen_raw.append(item)

        resolved, per_entry = resolve_playlist(items, master_data)

        for entry in per_entry:
            if not entry["valid_shape"]:
                findings.append({
                    "playlist": playlist_name, "code": "INVALID_ENTRY_SHAPE",
                    "detail": f"entry is neither a string nor an object: {entry['item']!r}",
                })
                continue

            if not _is_known_environment_pattern(entry["env_pattern"]):
                findings.append({
                    "playlist": playlist_name, "code": "UNKNOWN_ENVIRONMENT",
                    "detail": f"environment '{entry['env_pattern']}' (entry: {entry['item']!r})",
                })

            if entry["mode"] not in KNOWN_MODES:
                findings.append({
                    "playlist": playlist_name, "code": "UNKNOWN_MODE",
                    "detail": f"mode '{entry['mode']}' (entry: {entry['item']!r})",
                })

            if entry["match_count"] == 0:
                findings.append({
                    "playlist": playlist_name, "code": "ENTRY_NO_MATCHES",
                    "detail": f"no tracks matched entry: {entry['item']!r}",
                })

        if items and not resolved:
            findings.append({
                "playlist": playlist_name, "code": "EMPTY_RESOLUTION",
                "detail": "playlist resolved to zero tracks",
            })

    return findings


def _format_findings(findings):
    lines = [f"[{f['playlist']}] {f['code']}: {f['detail']}" for f in findings]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lint playlists.json against master_tracks_list.json")
    parser.add_argument("--playlists", default=DEFAULT_PLAYLISTS_PATH)
    parser.add_argument("--master", default=DEFAULT_MASTER_PATH)
    args = parser.parse_args(argv)

    if not os.path.exists(args.playlists):
        print(f"[trackcheck.lint_playlists] ERROR: playlists file not found at {args.playlists}")
        return 2
    if not os.path.exists(args.master):
        print(f"[trackcheck.lint_playlists] ERROR: master tracks list not found at {args.master}")
        return 2

    with open(args.playlists, "r") as f:
        playlists_data = json.load(f)
    with open(args.master, "r") as f:
        master_data = json.load(f)

    findings = lint_playlists(playlists_data, master_data)

    if findings:
        print(f"[trackcheck.lint_playlists] {len(findings)} issue(s) found:")
        print(_format_findings(findings))
        return 1

    print("[trackcheck.lint_playlists] OK: all playlists resolve cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
