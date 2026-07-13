"""Track geometry extraction — the common representation quality.py's metrics run on,
regardless of whether the track came from the generator (in-memory, pre-publish) or
from disk (a parsed .track/.race pair, e.g. a downloaded workshop item).

Reuses `generator/src/math_utils.py`'s `catmull_rom_spline` for length and turn-radius
estimation: fitting the same closed Catmull-Rom spline through the ordered gate
positions that the generator fits through its control points, then measuring arc
length and curvature along the dense sampled path, is a materially better estimate
than summing straight-line gate-to-gate distances (which is exactly the case for
turn radius — a "sharp" 90-degree waypoint on a smooth flight path is not the same
thing as a 90-degree corner). math_utils.py otherwise has no reusable metrics helpers
(it's spline generation + Euler-angle conversion, both about *generating* a path, not
scoring an already-materialized one) — this module adds distance/curvature/self-
intersection math of its own using numpy directly rather than force-fitting unrelated
helpers into math_utils.py (documented as a deliberate, minor deviation from the
feature doc's "reusing generator/src/math_utils.py" wording; see the feature doc).
"""

import math
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# generator/src is a plain (no __init__.py) namespace-style package that assumes
# "generator/" itself is on sys.path (see generator/main.py and pytest.ini's comment).
# trackcheck must be importable by callers whose cwd/sys.path setup we don't control
# (the workshop installer, the generator pipeline, pytest), so bootstrap it here
# rather than assuming the caller already did.
_GENERATOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generator")
if _GENERATOR_DIR not in sys.path:
    sys.path.insert(0, _GENERATOR_DIR)

from src.math_utils import catmull_rom_spline  # noqa: E402

from trackcheck.parser import parse_race_checkpoints, parse_track_blueprints  # noqa: E402


@dataclass
class TrackGeometry:
    """Ordered, deduplicated gate positions (racing order, loop NOT explicitly
    closed — i.e. no repeated trailing first-gate) plus the spawn position.
    Both `geometry_from_blueprints` and `geometry_from_files` normalize to this same
    shape so quality.py's metrics don't need to know which source produced them."""

    gate_positions: List[Tuple[float, float, float]]
    spawn_position: Optional[Tuple[float, float, float]]


def geometry_from_blueprints(blueprints, checkpoint_ids, spawn_id) -> TrackGeometry:
    """Build geometry directly from generate_procedural_track()'s in-memory return
    value — no disk round-trip needed. This is the path the future quality-gate-before-
    publish consumer (procedural-gen-improvements.md) and this package's own seed-batch
    tests use.
    """
    by_instance = {bp['instance_id']: bp for bp in blueprints}
    gate_positions = [tuple(by_instance[cid]['position']) for cid in checkpoint_ids if cid in by_instance]
    spawn_position = tuple(by_instance[spawn_id]['position']) if spawn_id in by_instance else None
    return TrackGeometry(gate_positions=gate_positions, spawn_position=spawn_position)


def geometry_from_files(track_path, race_path) -> Optional[TrackGeometry]:
    """Build geometry from a parsed .track + .race file pair. Gate order comes from
    the .race file's checkPointPassages sequence (the racing-order ground truth), not
    from the .track file's TrackBlueprint declaration order, which is not guaranteed
    to match lap order for arbitrary (e.g. downloaded) content. Returns None if either
    file fails to parse.
    """
    blueprints = parse_track_blueprints(track_path)
    race_info = parse_race_checkpoints(race_path)
    if blueprints is None or race_info is None:
        return None

    by_instance = {bp['instance_id']: bp for bp in blueprints}

    sequence = list(race_info['checkpoint_sequence'])
    # This codebase's own race writer (generator/src/io.py) always closes the lap by
    # repeating the first checkpoint id as the Finish passage. Drop that trailing
    # repeat so gate_positions is a simple "N unique ordered gates" list regardless of
    # source — quality.py's metrics close the loop themselves when needed (e.g. total
    # lap length includes the segment back to the first gate).
    if len(sequence) >= 2 and sequence[-1] == sequence[0]:
        sequence = sequence[:-1]

    gate_positions = [tuple(by_instance[cid]['position']) for cid in sequence if cid in by_instance]

    spawn_id = race_info['spawn_point_id']
    spawn_position = tuple(by_instance[spawn_id]['position']) if spawn_id in by_instance else None

    return TrackGeometry(gate_positions=gate_positions, spawn_position=spawn_position)


def _segments_intersect_2d(p1, p2, p3, p4):
    """Standard orientation-based 2D segment intersection test (proper crossing
    only — shared endpoints/collinear touches are not counted, since adjacent flight
    path segments always share an endpoint and that's not a self-intersection)."""

    def orientation(a, b, c):
        val = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if val > 1e-9:
            return 1
        if val < -1e-9:
            return -1
        return 0

    o1 = orientation(p1, p2, p3)
    o2 = orientation(p1, p2, p4)
    o3 = orientation(p3, p4, p1)
    o4 = orientation(p3, p4, p2)

    return o1 != o2 and o3 != o4 and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0


def compute_path_metrics(gate_positions: Sequence[Tuple[float, float, float]], num_points_per_segment: int = 12):
    """Fit a closed Catmull-Rom spline through `gate_positions` (same helper the
    generator itself uses) and derive:

      - total_length_m: sum of dense segment lengths around the closed loop
      - min_turn_radius_m: smallest curvature radius estimated from consecutive
        tangent vectors (radius ~= arc_length / turn_angle for small angles),
        math.inf if the path never turns
      - self_intersects: True if the dense 2D (X/Z-plane) flight path crosses
        itself at points more than 3m apart in height (a genuine crossing, not a
        legitimate bridge/tunnel over/under)

    Requires at least 4 gates (catmull_rom_spline's own minimum) — callers with
    fewer gates should treat that as an automatic quality-gate failure (see
    quality.py's MIN_GATES threshold) rather than calling this.
    """
    if len(gate_positions) < 4:
        raise ValueError("compute_path_metrics needs at least 4 gate positions (catmull_rom_spline minimum)")

    control_points = np.array(gate_positions, dtype=float)
    pts, tangents = catmull_rom_spline(control_points, num_points_per_segment=num_points_per_segment, closed=True)

    # Close the loop for length purposes (last dense point back to the first).
    closed_pts = np.vstack([pts, pts[0]])
    seg_lengths = np.linalg.norm(np.diff(closed_pts, axis=0), axis=1)
    total_length_m = float(seg_lengths.sum())

    closed_tangents = np.vstack([tangents, tangents[0]])
    min_turn_radius_m = math.inf
    for i in range(len(tangents)):
        t0, t1 = closed_tangents[i], closed_tangents[i + 1]
        cos_theta = float(np.clip(np.dot(t0, t1), -1.0, 1.0))
        theta = math.acos(cos_theta)
        if theta < 1e-6:
            continue  # effectively straight -- no meaningful turn radius here
        radius = seg_lengths[i] / theta
        if radius < min_turn_radius_m:
            min_turn_radius_m = radius

    self_intersects = _detect_self_intersection(pts)

    return {
        'total_length_m': total_length_m,
        'min_turn_radius_m': min_turn_radius_m,
        'self_intersects': self_intersects,
    }


def _detect_self_intersection(pts, height_tolerance_m: float = 3.0) -> bool:
    n = len(pts)
    xz = pts[:, (0, 2)]
    for i in range(n):
        a1, a2 = xz[i], xz[(i + 1) % n]
        for j in range(i + 2, n):
            # Skip the segment adjacent to i on the wraparound side (shares an endpoint).
            if i == 0 and j == n - 1:
                continue
            b1, b2 = xz[j], xz[(j + 1) % n]
            if _segments_intersect_2d(a1, a2, b1, b2):
                # Confirm it's a genuine 3D crossing, not a legitimate over/under
                # (bridge/tunnel) at a similar XZ location but different height.
                y_i = (pts[i][1] + pts[(i + 1) % n][1]) / 2.0
                y_j = (pts[j][1] + pts[(j + 1) % n][1]) / 2.0
                if abs(y_i - y_j) < height_tolerance_m:
                    return True
    return False
