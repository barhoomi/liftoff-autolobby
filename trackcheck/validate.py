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
from typing import Dict, List, Optional

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
    # Race-only items (validate_item_set): a workshop item that ships a .race and no
    # .track is legitimate -- Liftoff publishes tracks and races as separate items --
    # so it gets its own verdicts instead of TRACK_FILE_NOT_FOUND.
    RACE_TRACK_DEP_MISSING = "RACE_TRACK_DEP_MISSING"
    RACE_FILE_UNPARSEABLE = "RACE_FILE_UNPARSEABLE"


@dataclass
class Report:
    """Result of validate_item(). `ok` is True iff `reasons` is empty.

    `warnings` is deliberately a separate list, not a subset of `reasons`: a warned
    condition is one the caller opted out of blocking on (see validate_item's
    `require_race` / `require_gates`), and folding the two together would make
    `report.reasons` mean two different things depending on the caller's flags.
    """

    item_dir: str
    ok: bool
    local_id: Optional[str] = None
    name: Optional[str] = None
    environment: Optional[str] = None
    track_path: Optional[str] = None
    race_paths: List[str] = field(default_factory=list)
    reasons: List[Reason] = field(default_factory=list)
    warnings: List[Reason] = field(default_factory=list)

    def reject(self, reason: Reason) -> None:
        self.ok = False
        if reason not in self.reasons:
            self.reasons.append(reason)

    def warn(self, reason: Reason) -> None:
        """Record a non-blocking finding. Never clears `ok`."""
        if reason not in self.warnings:
            self.warnings.append(reason)


def _find_track_files(item_dir):
    return sorted(glob.glob(os.path.join(item_dir, "**", "*.track"), recursive=True))


def _find_race_files(search_dirs):
    races = []
    for d in search_dirs:
        races.extend(sorted(glob.glob(os.path.join(d, "**", "*.race"), recursive=True)))
    return races


def validate_item(item_dir, race_search_dirs=None, require_race=True,
                  require_gates=True) -> Report:
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

    `require_race=False` / `require_gates=False` downgrade NO_MATCHING_RACE and
    GATE_DATA_MISSING from rejections to `report.warnings` entries (see
    docs/features/doing/workshop-ingest-hardening.md §7): a gateless "freestyle" track is
    perfectly flyable in the non-race modes the game offers it in, and which modes those
    are is a question the game already answers through the plugin's availability sweep —
    so trackcheck stops guessing at it instead of growing a second mode vocabulary. Both
    default to True, so every existing caller sees byte-identical behaviour.
    SPAWN_DATA_MISSING stays blocking either way: a track with no spawn point cannot be
    flown in any mode at all.
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
        if require_gates:
            report.reject(Reason.GATE_DATA_MISSING)
        else:
            report.warn(Reason.GATE_DATA_MISSING)
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
            if require_race:
                report.reject(Reason.NO_MATCHING_RACE)
            else:
                report.warn(Reason.NO_MATCHING_RACE)

    return report


def _find_race_files_in(item_dir):
    return sorted(glob.glob(os.path.join(item_dir, "**", "*.race"), recursive=True))


def _track_local_ids(search_dirs):
    """Every localID reachable from `search_dirs`, so a race's TRACK dependency can be
    resolved against tracks that live in a *sibling* item."""
    local_ids = set()
    for d in search_dirs:
        for track_path in _find_track_files(d):
            parsed = parse_track_file(track_path)
            if parsed and parsed[0]:
                local_ids.add(parsed[0])
    return local_ids


def _validate_race_only_item(item_dir, race_files, known_track_ids) -> Report:
    """A workshop item that ships races and no track.

    Liftoff publishes a track and its race as *separate* workshop items, so this is a
    normal thing to download, not a corrupt item — but it is only usable if the track it
    depends on is somewhere we can see. `report.name` / `report.local_id` describe the
    race and its TRACK dependency (there is no track here to describe), and `environment`
    stays None because a .race does not declare one.
    """
    report = Report(item_dir=item_dir, ok=True)

    first_name = None
    first_dep = None
    matched = False
    for race_path in race_files:
        parsed = parse_race_file(race_path)
        if parsed is None:
            report.reject(Reason.RACE_FILE_UNPARSEABLE)
            continue
        race_name, track_dep = parsed
        if first_name is None and first_dep is None:
            first_name, first_dep = race_name, track_dep
        if track_dep and track_dep in known_track_ids:
            if not matched:
                report.name, report.local_id = race_name, track_dep
                matched = True
            report.race_paths.append(race_path)

    if not matched:
        report.name, report.local_id = first_name, first_dep
        report.reject(Reason.RACE_TRACK_DEP_MISSING)

    return report


def validate_item_set(item_dirs, race_search_dirs=None, require_race=True,
                      require_gates=True) -> Dict[str, Report]:
    """Validate several item directories *as one set*, keyed by item dir.

    This is what an ingest of `/dl <track_id> <race_id>` needs and what validating each
    directory on its own cannot do: Liftoff ships a track and its race as separate
    workshop items, so item-at-a-time validation deadlocks — the track alone fails
    NO_MATCHING_RACE, the race alone has no .track at all and gets quarantined, after
    which the track can never validate. Validating the set means a race published in a
    *sibling item of the same batch* satisfies its partner's race requirement.

    Each directory is classified as:

    - a **track item** (it contains at least one .track) — validated exactly as
      `validate_item` would, with `race_search_dirs` and the two flags forwarded;
    - a **race-only item** (no .track, at least one .race) — usable iff one of its races'
      TRACK dependency resolves to a .track anywhere in `race_search_dirs`;
    - an **empty item** (neither) — TRACK_FILE_NOT_FOUND, exactly as today.

    `race_search_dirs` defaults to the item dirs themselves; real callers extend it (the
    workshop ingest adds the whole content root, so an already-installed partner counts).
    """
    item_dirs = list(item_dirs)
    if race_search_dirs is None:
        race_search_dirs = list(item_dirs)
    else:
        race_search_dirs = list(race_search_dirs)

    known_track_ids = None  # resolved lazily: only race-only items need it
    reports = {}
    for item_dir in item_dirs:
        if _find_track_files(item_dir):
            reports[item_dir] = validate_item(item_dir, race_search_dirs=race_search_dirs,
                                              require_race=require_race,
                                              require_gates=require_gates)
            continue

        race_files = _find_race_files_in(item_dir)
        if not race_files:
            report = Report(item_dir=item_dir, ok=True)
            report.reject(Reason.TRACK_FILE_NOT_FOUND)
            reports[item_dir] = report
            continue

        if known_track_ids is None:
            known_track_ids = _track_local_ids(race_search_dirs)
        reports[item_dir] = _validate_race_only_item(item_dir, race_files, known_track_ids)

    return reports
