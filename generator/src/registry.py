"""generator/src/registry.py -- published_tracks.json: the generator's single
source of truth for what it has shipped to Steam Workshop.

Per docs/features/doing/procedural-gen-improvements.md ("Known gap" #3) and
AGENTS.md rule 4 (single source of truth for state files): this registry, not
master_tracks_list.json, is where {workshop_id, name, seed, content_hash, tags,
environment, published_at} live. master_tracks_list.json is gitignored and
regenerated at runtime by gather_tracks.py from a live game install -- it only
ever reflects what is currently *installed* on the bot. This registry is
checked into git and reflects what the generator has *published*, which is a
different fact with a different lifetime (an item can be published and later
fall out of the bot's install without this registry needing to change).
"""

import hashlib
import json
import os
from typing import Dict, List, Optional

PROJECT_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_REGISTRY_PATH = os.path.join(PROJECT_WORKSPACE, "published_tracks.json")

REGISTRY_VERSION = 1


def load_registry(path: str = DEFAULT_REGISTRY_PATH) -> Dict:
    """Load the registry, tolerating a missing file (fresh checkout / first run
    ever) by returning an empty-but-well-formed registry rather than raising."""
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        data.setdefault("version", REGISTRY_VERSION)
        data.setdefault("tracks", [])
        return data
    return {"version": REGISTRY_VERSION, "tracks": []}


def save_registry(registry: Dict, path: str = DEFAULT_REGISTRY_PATH) -> None:
    """Persist the registry as pretty, diff-friendly JSON (sorted keys off --
    insertion order of the `tracks` list is meaningful: publish order)."""
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


def compute_content_hash(geometry) -> str:
    """Deterministic sha256 over a track's gate + spawn geometry -- the dedupe
    key. Two candidates with the same content hash are the same physical track
    layout regardless of track_id/seed bookkeeping, so a re-run of the batch
    pipeline that regenerates an already-published layout is recognized and
    skipped rather than uploaded again as a duplicate workshop item (see
    generate_batch.py's dedupe step and its acceptance-criterion test).

    `geometry` is any object exposing `.gate_positions` (iterable of (x, y, z))
    and `.spawn_position` (an (x, y, z) or None) -- i.e. a
    trackcheck.geometry.TrackGeometry, without importing that type here (keeps
    this module import-light and duck-typed against either the in-memory or
    from-disk geometry source).

    Coordinates are rounded to 3 decimal places before hashing: generation is
    deterministic for a given seed+params, but rounding guards against float
    representation drift (e.g. a numpy version bump) producing a spurious "new"
    hash for what is, physically, the identical track.
    """
    gates = [[round(float(c), 3) for c in pos] for pos in geometry.gate_positions]
    spawn = [round(float(c), 3) for c in geometry.spawn_position] if geometry.spawn_position else None
    payload = {"gates": gates, "spawn": spawn}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def find_by_content_hash(registry: Dict, content_hash: str) -> Optional[Dict]:
    """Return the existing registry entry with this content hash, or None."""
    for entry in registry.get("tracks", []):
        if entry.get("content_hash") == content_hash:
            return entry
    return None


def make_entry(
    *,
    workshop_id: str,
    track_id: str,
    name: str,
    seed: int,
    content_hash: str,
    tags: Dict[str, str],
    environment: str,
    published_at: str,
) -> Dict:
    """Build one registry row. Field set matches the feature doc's two (slightly
    differently worded) lists: `{workshop_id, difficulty, style, seed, hash}`
    from the pipeline diagram and `(id, name, seed, content hash, tags,
    environment, published_at)` from "Known gap" #3 -- reconciled here as
    `tags: {"difficulty": ..., "style": ...}` (trackcheck.classify()'s own
    return shape) plus `track_id`, since publish.py / io.py key the on-disk
    Tracks/Races folders by track_id and it's needed to re-stage or
    republish-in-place later.
    """
    return {
        "workshop_id": workshop_id,
        "track_id": track_id,
        "name": name,
        "seed": seed,
        "content_hash": content_hash,
        "tags": dict(tags),
        "environment": environment,
        "published_at": published_at,
    }


def append_entry(registry: Dict, entry: Dict) -> Dict:
    """Append `entry` to `registry["tracks"]` in place and return the registry
    (for chaining); does not save to disk -- call save_registry() separately."""
    registry.setdefault("tracks", []).append(entry)
    return registry
