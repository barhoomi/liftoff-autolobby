"""Layer 1 (correctness) — validate_item(): is this track/race pair even correct?

Every rejection carries a machine-readable reason code (Reason, a str Enum) so
callers (the quarantine path, structured logs, test assertions) never have to parse
prose. A Report is produced regardless of pass/fail — `report.ok` is the fast path,
`report.reasons` is populated only on failure.
"""

import glob
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from trackcheck.parser import (
    is_known_environment,
    normalize_env,
    parse_race_file,
    parse_track_blueprints,
    parse_track_file,
)

# Item ids that count as "a gate" / "a spawn point" for the gate/spawn-data-present
# check. Kept as a small local allowlist rather than importing generator/src/assets.py:
# workshop/downloaded tracks use gate assets the generator never emits (e.g.
# CheckpointBox5mX5m01, InflatableArchBrandless01 — see generator/src/assets.py's own
# comments), so the real-world set of valid gate ids is broader than what our own
# generator happens to produce. Matching is substring-based ("Gate" / "Checkpoint" in
# the item id) rather than an exact allowlist, since new gate props get added to the
# game over time and this check only needs to answer "is there *something* gate-like
# placed on this track", not identify which specific asset.
_GATE_ID_HINTS = ("gate", "checkpoint")
_SPAWN_ID_HINTS = ("spawnpoint",)

# A `<b>`/`<color=...>` (or any other tag-shaped) substring in a track name would
# corrupt SplitMessage's tag-tracking when the name is echoed in chat (see AGENTS.md's
# "Chat output constraints"). Reject anything that looks like a markup tag.
_MARKUP_TAG_RE = re.compile(r'<[^>]*>')


class Reason(str, Enum):
    """Machine-readable rejection reason codes. str subclass so `reason == "CODE"`
    and JSON serialization both work without extra plumbing."""

    TRACK_FILE_NOT_FOUND = "TRACK_FILE_NOT_FOUND"
    TRACK_FILE_UNPARSEABLE = "TRACK_FILE_UNPARSEABLE"
    LOCAL_ID_MISSING = "LOCAL_ID_MISSING"
    NAME_MISSING = "NAME_MISSING"
    NAME_UNSAFE_MARKUP = "NAME_UNSAFE_MARKUP"
    ENVIRONMENT_MISSING = "ENVIRONMENT_MISSING"
    ENVIRONMENT_UNSUPPORTED = "ENVIRONMENT_UNSUPPORTED"
    NO_MATCHING_RACE = "NO_MATCHING_RACE"
    GATE_DATA_MISSING = "GATE_DATA_MISSING"
    SPAWN_DATA_MISSING = "SPAWN_DATA_MISSING"


@dataclass
class Report:
    """Result of validate_item(). `ok` is True iff `reasons` is empty."""

    item_dir: str
    ok: bool
    local_id: Optional[str] = None
    name: Optional[str] = None
    environment: Optional[str] = None
    track_path: Optional[str] = None
    race_paths: List[str] = field(default_factory=list)
    reasons: List[Reason] = field(default_factory=list)

    def reject(self, reason: Reason) -> None:
        self.ok = False
        if reason not in self.reasons:
            self.reasons.append(reason)


def _find_track_files(item_dir):
    return sorted(glob.glob(os.path.join(item_dir, "**", "*.track"), recursive=True))


def _find_race_files(search_dirs):
    races = []
    for d in search_dirs:
        races.extend(sorted(glob.glob(os.path.join(d, "**", "*.race"), recursive=True)))
    return races


def validate_item(item_dir, race_search_dirs=None) -> Report:
    """Validate a single track "item" (a directory containing at least one .track
    file — a workshop item directory, or a local Tracks/<id>/ directory).

    Checks (see docs/features/doing/track-validation-quality-gate.md, Layer 1):
      - the track file parses at all
      - it has a localID, a name, and a declared environment
      - the environment is a known/supported one (reuses ENV_MAPPING)
      - at least one .race file (searched under `race_search_dirs`, default:
        [item_dir] itself) whose TRACK dependency matches this track's localID
      - gate/spawn blueprint data is present
      - the name is non-empty and free of chat-breaking markup

    `race_search_dirs` defaults to [item_dir] so a self-contained fixture (a single
    directory holding both the .track and its .race, as in trackcheck/tests/fixtures)
    validates with no extra wiring. Real callers with a split layout (e.g. Liftoff's
    sibling Tracks/ and Races/ directories, or a whole workshop content root where
    tracks and races live in different numbered item directories — see the parser.py
    docstring) should pass the actual directories to search.

    If an item directory contains multiple versioned .track files (workshop items are
    often re-uploaded as `<localID>_0001.track`, `_0002.track`, ...), the
    lexicographically last filename is treated as canonical (highest version suffix
    wins) — in practice all versions share the same localID/name/environment.
    """
    report = Report(item_dir=item_dir, ok=True)

    if race_search_dirs is None:
        race_search_dirs = [item_dir]

    track_files = _find_track_files(item_dir)
    if not track_files:
        report.reject(Reason.TRACK_FILE_NOT_FOUND)
        return report

    track_path = track_files[-1]
    report.track_path = track_path

    parsed = parse_track_file(track_path)
    if parsed is None:
        report.reject(Reason.TRACK_FILE_UNPARSEABLE)
        return report

    local_id, name, env_raw = parsed
    report.local_id = local_id
    report.name = name

    if not local_id:
        report.reject(Reason.LOCAL_ID_MISSING)

    if not name:
        report.reject(Reason.NAME_MISSING)
    elif _MARKUP_TAG_RE.search(name):
        report.reject(Reason.NAME_UNSAFE_MARKUP)

    if not env_raw:
        report.reject(Reason.ENVIRONMENT_MISSING)
    elif not is_known_environment(env_raw):
        report.reject(Reason.ENVIRONMENT_UNSUPPORTED)
    else:
        report.environment = normalize_env(env_raw)

    # Gate/spawn presence: parsed independently of the localID/name/env fields above
    # so a track that's otherwise fine but has no playable content still gets a
    # specific reason rather than being silently marked ok.
    blueprints = parse_track_blueprints(track_path)
    if blueprints is None:
        # Already know the file parses (parse_track_file succeeded above with the
        # same underlying parse_xml_robust) — treat as "no blueprints" rather than
        # re-flagging as unparseable.
        blueprints = []

    has_gate = any(
        bp['item_id'] and any(hint in bp['item_id'].lower() for hint in _GATE_ID_HINTS)
        for bp in blueprints
    )
    has_spawn = any(
        bp['item_id'] and any(hint in bp['item_id'].lower() for hint in _SPAWN_ID_HINTS)
        for bp in blueprints
    )
    if not has_gate:
        report.reject(Reason.GATE_DATA_MISSING)
    if not has_spawn:
        report.reject(Reason.SPAWN_DATA_MISSING)

    # Matching race: only meaningful once we have a local_id to match against.
    if local_id:
        race_files = _find_race_files(race_search_dirs)
        found_match = False
        for race_path in race_files:
            race_parsed = parse_race_file(race_path)
            if race_parsed is None:
                continue
            _race_name, track_dep = race_parsed
            if track_dep == local_id:
                found_match = True
                report.race_paths.append(race_path)
        if not found_match:
            report.reject(Reason.NO_MATCHING_RACE)

    return report
