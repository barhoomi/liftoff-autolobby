"""Layer 2 (quality) — is this correct track any good?

Operates on a `TrackGeometry` (see geometry.py), so it's agnostic to whether the
track was just generated in-memory or parsed from a downloaded workshop item.
`quality_gate()` is a hard pass/fail against configurable thresholds; `classify()`
derives soft difficulty/style tags for playlist selectors, independent of whether
the gate passed.
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from trackcheck.geometry import TrackGeometry, compute_path_metrics

# Thresholds are a plain dict (not a dataclass) per the feature doc's wording ("a
# config dict") -- callers can pass a partial override dict and unspecified keys
# fall back to these defaults (see quality_gate()).
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "min_gates": 4,
    "min_length_m": 80.0,
    "max_length_m": 4000.0,
    "min_turn_radius_m": 3.0,
    "max_elevation_delta_m": 250.0,
    "allow_self_intersection": False,
}


class QualityReason(str, Enum):
    TOO_FEW_GATES = "TOO_FEW_GATES"
    TRACK_TOO_SHORT = "TRACK_TOO_SHORT"
    TRACK_TOO_LONG = "TRACK_TOO_LONG"
    TURN_RADIUS_TOO_TIGHT = "TURN_RADIUS_TOO_TIGHT"
    ELEVATION_DELTA_TOO_LARGE = "ELEVATION_DELTA_TOO_LARGE"
    SELF_INTERSECTING = "SELF_INTERSECTING"


@dataclass
class QualityMetrics:
    gate_count: int
    total_length_m: float
    elevation_delta_m: float
    min_turn_radius_m: float
    self_intersects: bool


@dataclass
class QualityResult:
    passed: bool
    reasons: List[QualityReason] = field(default_factory=list)
    metrics: "QualityMetrics" = None


def compute_metrics(geometry: TrackGeometry) -> QualityMetrics:
    """Compute the Layer 2 metrics for a track's geometry: gate count, total lap
    length, elevation delta, minimum turn radius, and self-intersection."""
    gate_count = len(geometry.gate_positions)

    if gate_count == 0:
        return QualityMetrics(
            gate_count=0, total_length_m=0.0, elevation_delta_m=0.0,
            min_turn_radius_m=math.inf, self_intersects=False,
        )

    elevations = [p[1] for p in geometry.gate_positions]
    elevation_delta_m = max(elevations) - min(elevations)

    if gate_count < 4:
        # catmull_rom_spline needs >= 4 control points; a track this sparse is
        # already going to fail the gate-count threshold, so length/turn-radius/
        # self-intersection just report as "unknown" rather than raising.
        return QualityMetrics(
            gate_count=gate_count, total_length_m=0.0, elevation_delta_m=elevation_delta_m,
            min_turn_radius_m=math.inf, self_intersects=False,
        )

    path_metrics = compute_path_metrics(geometry.gate_positions)
    return QualityMetrics(
        gate_count=gate_count,
        total_length_m=path_metrics['total_length_m'],
        elevation_delta_m=elevation_delta_m,
        min_turn_radius_m=path_metrics['min_turn_radius_m'],
        self_intersects=path_metrics['self_intersects'],
    )


def quality_gate(geometry: TrackGeometry, thresholds: Dict[str, float] = None) -> QualityResult:
    """Hard pass/fail against `thresholds` (defaults to DEFAULT_THRESHOLDS; a partial
    dict overrides just those keys). Every failure reason is recorded (not just the
    first one), so a track can fail on multiple grounds simultaneously."""
    merged = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        merged.update(thresholds)

    metrics = compute_metrics(geometry)
    reasons: List[QualityReason] = []

    if metrics.gate_count < merged["min_gates"]:
        reasons.append(QualityReason.TOO_FEW_GATES)
    if metrics.total_length_m < merged["min_length_m"]:
        reasons.append(QualityReason.TRACK_TOO_SHORT)
    if metrics.total_length_m > merged["max_length_m"]:
        reasons.append(QualityReason.TRACK_TOO_LONG)
    if metrics.min_turn_radius_m < merged["min_turn_radius_m"]:
        reasons.append(QualityReason.TURN_RADIUS_TOO_TIGHT)
    if metrics.elevation_delta_m > merged["max_elevation_delta_m"]:
        reasons.append(QualityReason.ELEVATION_DELTA_TOO_LARGE)
    if metrics.self_intersects and not merged["allow_self_intersection"]:
        reasons.append(QualityReason.SELF_INTERSECTING)

    return QualityResult(passed=not reasons, reasons=reasons, metrics=metrics)


def classify(metrics: QualityMetrics) -> Dict[str, str]:
    """Derive {"difficulty": easy|medium|hard, "style": flow|technical} tags from
    gate density, turn radius, and elevation change. Heuristic thresholds, tuned by
    eyeballing the generator's own PRESETS (generator/generate_batch.py) output
    rather than any in-game skill data -- documented as such in the feature doc.
    Safe to call on a track that failed quality_gate(); it's just descriptive.
    """
    gate_count = metrics.gate_count
    length = metrics.total_length_m
    turn_radius = metrics.min_turn_radius_m
    elevation_delta = metrics.elevation_delta_m

    gate_density = (gate_count / (length / 100.0)) if length > 0 else 0.0

    # Style: tight turns and/or dense gates read as "technical"; a spacious, sweeping
    # layout reads as "flow".
    style = "technical" if (turn_radius < 12.0 or gate_density > 3.0) else "flow"

    # Difficulty: combine turn tightness and elevation change into a simple score.
    difficulty_score = 0
    if turn_radius < 6.0:
        difficulty_score += 2
    elif turn_radius < 15.0:
        difficulty_score += 1
    if elevation_delta > 40.0:
        difficulty_score += 2
    elif elevation_delta > 15.0:
        difficulty_score += 1
    if gate_density > 4.0:
        difficulty_score += 1

    if difficulty_score >= 3:
        difficulty = "hard"
    elif difficulty_score >= 1:
        difficulty = "medium"
    else:
        difficulty = "easy"

    return {"difficulty": difficulty, "style": style}
