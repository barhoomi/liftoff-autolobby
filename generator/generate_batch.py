"""generator/generate_batch.py -- the unattended content pipeline.

Grown (per docs/features/doing/procedural-gen-improvements.md) from "shell out to
main.py --publish once per hand-tuned preset" into the full pipeline the feature
doc describes:

    seeds -> generate N candidates (in-memory, no disk writes yet)
          -> trackcheck.quality_gate      (reject bad layouts)
          -> dedupe by content hash        (never re-upload an identical track)
          -> trackcheck.classify           (difficulty/style tags)
          -> publish each keeper as its own NEW workshop item
          -> append {workshop_id, tags, seed, hash, ...} to published_tracks.json,
             saved after every single publish (crash-resilient: a failure
             mid-batch never loses already-published tracks' records)

All real-world side effects -- writing track/race files under the Liftoff
install, and the steamcmd publish itself -- are injectable (`save_fn`,
`publish_fn`) so the whole pipeline is unit-testable without touching disk or
Steam. See generator/tests/test_generate_batch.py. Running this script for real
(the default `python3 generate_batch.py` with no --dry-run) is an operator
action, never something this feature's own test suite or CI does.
"""

import argparse
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.generator import generate_procedural_track
from src.io import save_track_and_race
from src.publish import PublishError
from src.publish import publish_track as _real_publish_track
from src.registry import (
    DEFAULT_REGISTRY_PATH,
    append_entry,
    compute_content_hash,
    find_by_content_hash,
    load_registry,
    make_entry,
    save_registry,
)

from trackcheck.geometry import geometry_from_blueprints
from trackcheck.quality import classify, quality_gate

# Hand-tuned generation presets. PRESETS[0] (circle/8pts/r=45/spacing=18) is the
# one trackcheck's own calibration measured at 0/100 rejections (see
# docs/features/done/track-validation-quality-gate.md) -- deliberately kept
# first so a default/small batch is unlikely to be an all-rejects run.
PRESETS = [
    {"shape": "circle", "points": 8, "radius": 45.0, "elevation": 4.0},
    {"shape": "triangle", "points": 6, "radius": 55.0, "elevation": 6.0},
    {"shape": "square", "points": 10, "radius": 40.0, "elevation": 3.0},
    {"shape": "circle", "points": 12, "radius": 50.0, "elevation": 7.0},
    {"shape": "triangle", "points": 8, "radius": 35.0, "elevation": 2.0},
    {"shape": "square", "points": 6, "radius": 60.0, "elevation": 5.0},
    {"shape": "circle", "points": 10, "radius": 45.0, "elevation": 6.0},
    {"shape": "triangle", "points": 12, "radius": 55.0, "elevation": 4.0},
    {"shape": "square", "points": 8, "radius": 50.0, "elevation": 7.0},
    {"shape": "circle", "points": 8, "radius": 40.0, "elevation": 5.0},
]

DEFAULT_ENVIRONMENT = "TheDrawingBoard"
DEFAULT_LAPS = 3
DEFAULT_GATE_SPACING = 18.0


@dataclass
class BatchResult:
    """Summary of one run_batch() call. Every list holds dicts describing what
    happened to that candidate, so callers/tests can assert exact reasons, not
    just counts."""

    generated: List[Dict] = field(default_factory=list)
    rejected: List[Dict] = field(default_factory=list)
    duplicates: List[Dict] = field(default_factory=list)
    published: List[Dict] = field(default_factory=list)
    errors: List[Dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.generated)


def generate_candidate(track_id, seed, preset, *, gate_spacing=DEFAULT_GATE_SPACING):
    """Generate one candidate fully in-memory (no disk writes) and build its
    trackcheck geometry. The quality gate and content hash both run on this
    return value before anything is written to disk or staged for upload."""
    blueprints, checkpoint_ids, spawn_id = generate_procedural_track(
        seed=seed,
        num_control_points=preset["points"],
        radius=preset["radius"],
        gate_spacing=gate_spacing,
        elevation_amplitude=preset["elevation"],
        shape=preset["shape"],
    )
    geometry = geometry_from_blueprints(blueprints, checkpoint_ids, spawn_id)
    return blueprints, checkpoint_ids, spawn_id, geometry


def run_batch(
    count,
    *,
    registry_path=DEFAULT_REGISTRY_PATH,
    thresholds=None,
    environment=DEFAULT_ENVIRONMENT,
    laps=DEFAULT_LAPS,
    gate_spacing=DEFAULT_GATE_SPACING,
    presets=None,
    rng=None,
    seed_fn=None,
    save_fn=None,
    publish_fn=None,
    dry_run=False,
    id_offset=0,
):
    """
    Generate `count` candidates -> quality_gate -> dedupe by content hash
    against the persisted registry -> classify -> publish each keeper as a new
    workshop item -> append to the registry (saved after every publish).

    Injectable real-world seams (both default to the real, disk/Steam-touching
    implementations -- tests always override both):
      - `save_fn(track_id, display_name, environment, blueprints,
         checkpoint_ids, spawn_point_id, laps) -> (track_path, race_path)`,
        defaults to `src.io.save_track_and_race`.
      - `publish_fn(track_id) -> workshop_id`, defaults to
        `src.publish.publish_track` (which invokes steamcmd via its own
        default runner). May raise `PublishError`.

    `seed_fn(i) -> int` overrides seed generation per candidate index (falls
    back to `rng.randint(1, 100000)`); pass a fixed sequence to make a test
    (or an operator re-run) deterministic and reproducible.

    `dry_run=True` runs generation/gate/dedupe/classify but skips `save_fn`
    and `publish_fn` entirely -- nothing is written to disk, nothing is
    "published", and nothing is appended to the registry. Useful to preview a
    batch. Default False matches the spec's "unattended batch run"; actually
    invoking this with dry_run=False against the real save_fn/publish_fn
    defaults is an operator action, never something this feature's test suite
    does.
    """
    presets = presets or PRESETS
    rng = rng or random.Random()
    seed_fn = seed_fn or (lambda i: rng.randint(1, 100000))
    save = save_fn or save_track_and_race
    publish = publish_fn or _real_publish_track

    registry = load_registry(registry_path)
    result = BatchResult()

    print(f"[Batch] Starting generation of {count} candidate tracks...")

    for i in range(count):
        preset = presets[i % len(presets)]
        n = id_offset + i + 1
        track_id = f"proc_batch_{n}"
        track_name = f"Procedural Batch {n}"
        seed = seed_fn(i)

        print(f"\n[Batch] [{i + 1}/{count}] Generating {track_name} (seed={seed}, shape={preset['shape']})...")

        blueprints, checkpoint_ids, spawn_id, geometry = generate_candidate(
            track_id, seed, preset, gate_spacing=gate_spacing,
        )
        result.generated.append({"track_id": track_id, "name": track_name, "seed": seed})

        quality_result = quality_gate(geometry, thresholds)
        if not quality_result.passed:
            reasons = [r.value for r in quality_result.reasons]
            print(f"[Batch] REJECTED {track_id}: {reasons}")
            result.rejected.append({"track_id": track_id, "seed": seed, "reasons": reasons})
            continue

        content_hash = compute_content_hash(geometry)
        existing = find_by_content_hash(registry, content_hash)
        if existing is not None:
            print(
                f"[Batch] DUPLICATE {track_id}: content hash matches already-published "
                f"{existing.get('workshop_id')} ({existing.get('track_id')}) -- skipping upload."
            )
            result.duplicates.append({
                "track_id": track_id, "seed": seed, "content_hash": content_hash,
                "matches_workshop_id": existing.get("workshop_id"),
            })
            continue

        tags = classify(quality_result.metrics)

        if dry_run:
            print(f"[Batch] DRY-RUN keep {track_id}: tags={tags} (not saved or published)")
            continue

        try:
            save(
                track_id=track_id,
                display_name=track_name,
                environment=environment,
                blueprints=blueprints,
                checkpoint_ids=checkpoint_ids,
                spawn_point_id=spawn_id,
                laps=laps,
            )
            workshop_id = publish(track_id)
        except PublishError as e:
            print(f"[Batch] ERROR publishing {track_id}: {e}", file=sys.stderr)
            result.errors.append({"track_id": track_id, "seed": seed, "error": str(e)})
            continue
        except Exception as e:
            print(f"[Batch] ERROR generating/staging {track_id}: {e}", file=sys.stderr)
            result.errors.append({"track_id": track_id, "seed": seed, "error": str(e)})
            continue

        entry = make_entry(
            workshop_id=workshop_id,
            track_id=track_id,
            name=track_name,
            seed=seed,
            content_hash=content_hash,
            tags=tags,
            environment=environment,
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        append_entry(registry, entry)
        save_registry(registry, registry_path)  # persist after EVERY publish -- crash resilience
        result.published.append(entry)
        print(f"[Batch] PUBLISHED {track_id} -> workshop_id={workshop_id} tags={tags}")

    print(
        f"\n[Batch] Done. {len(result.published)} published, {len(result.rejected)} rejected, "
        f"{len(result.duplicates)} duplicate (skipped), {len(result.errors)} errors, "
        f"out of {result.total} candidates."
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Batch-generate, gate, dedupe and publish procedural tracks.")
    parser.add_argument("--count", type=int, default=len(PRESETS), help="Number of candidates to generate.")
    parser.add_argument("--env", default=DEFAULT_ENVIRONMENT, help="Liftoff environment name.")
    parser.add_argument("--laps", type=int, default=DEFAULT_LAPS)
    parser.add_argument("--registry", default=DEFAULT_REGISTRY_PATH, help="Path to published_tracks.json.")
    parser.add_argument("--seed", type=int, default=None, help="Base RNG seed (omit for a fresh random batch).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Gate/dedupe/classify only -- do not save track files or publish anything.",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    result = run_batch(
        args.count,
        registry_path=args.registry,
        environment=args.env,
        laps=args.laps,
        rng=rng,
        dry_run=args.dry_run,
    )

    if result.errors and not result.published and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
