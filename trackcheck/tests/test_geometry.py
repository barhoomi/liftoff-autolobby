import math
import os
import sys

import numpy as np
import pytest

from trackcheck.geometry import compute_path_metrics, geometry_from_blueprints, geometry_from_files

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
_GENERATOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "generator")
if _GENERATOR_DIR not in sys.path:
    sys.path.insert(0, _GENERATOR_DIR)
from src.generator import generate_procedural_track  # noqa: E402


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


class TestGeometryFromBlueprints:
    def test_gate_positions_follow_checkpoint_order(self):
        blueprints, checkpoint_ids, spawn_id = generate_procedural_track(seed=123, shape="circle", num_control_points=6, radius=28.0, gate_spacing=35.0)
        geometry = geometry_from_blueprints(blueprints, checkpoint_ids, spawn_id)
        assert len(geometry.gate_positions) == len(checkpoint_ids)

        by_instance = {bp["instance_id"]: bp for bp in blueprints}
        expected = [tuple(by_instance[cid]["position"]) for cid in checkpoint_ids]
        assert geometry.gate_positions == expected

    def test_spawn_position_matches_spawn_blueprint(self):
        blueprints, checkpoint_ids, spawn_id = generate_procedural_track(seed=5)
        geometry = geometry_from_blueprints(blueprints, checkpoint_ids, spawn_id)
        by_instance = {bp["instance_id"]: bp for bp in blueprints}
        assert geometry.spawn_position == tuple(by_instance[spawn_id]["position"])


class TestGeometryFromFiles:
    def test_matches_the_generator_source_geometry(self):
        # good_item's fixture was generated with seed=123/circle/6/28.0/35.0 -- rebuild
        # the same in-memory geometry and confirm the file-parsed path agrees, proving
        # the .race checkpoint-sequence-based ordering reconstructs the same lap order
        # the generator produced in the first place.
        blueprints, checkpoint_ids, spawn_id = generate_procedural_track(seed=123, shape="circle", num_control_points=6, radius=28.0, gate_spacing=35.0)
        expected = geometry_from_blueprints(blueprints, checkpoint_ids, spawn_id)

        parsed = geometry_from_files(
            fixture_path("good_item", "fixture_good.track"),
            fixture_path("good_item", "fixture_good_race_0001.race"),
        )
        assert parsed is not None
        assert len(parsed.gate_positions) == len(expected.gate_positions)
        for a, b in zip(parsed.gate_positions, expected.gate_positions):
            assert a == pytest.approx(b)
        assert parsed.spawn_position == pytest.approx(expected.spawn_position)

    def test_returns_none_on_unparseable_track(self):
        result = geometry_from_files(
            fixture_path("malformed_xml_item", "fixture_malformed.track"),
            fixture_path("good_item", "fixture_good_race_0001.race"),
        )
        assert result is None

    def test_finish_repeat_of_first_gate_is_dropped(self):
        parsed = geometry_from_files(
            fixture_path("good_item", "fixture_good.track"),
            fixture_path("good_item", "fixture_good_race_0001.race"),
        )
        # 5 gates were generated; the .race file records 6 passages (finish repeats
        # the first) -- geometry_from_files must dedupe that back down to 5.
        assert len(parsed.gate_positions) == 5


class TestComputePathMetrics:
    def test_requires_at_least_four_gates(self):
        with pytest.raises(ValueError):
            compute_path_metrics([(0.0, 5.0, 0.0), (10.0, 5.0, 0.0), (10.0, 5.0, 10.0)])

    def test_square_loop_length_is_close_to_perimeter(self):
        # A large, gently-perturbed square-ish loop's spline length should be in the
        # right ballpark of the straight-line perimeter (the spline overshoots a bit
        # since it curves outside the corners, but not by an order of magnitude).
        gates = [
            (20.0, 5.0, 20.0),
            (-20.0, 5.0, 20.0),
            (-20.0, 5.0, -20.0),
            (20.0, 5.0, -20.0),
        ]
        metrics = compute_path_metrics(gates)
        perimeter = 4 * 40.0
        assert perimeter * 0.8 < metrics["total_length_m"] < perimeter * 1.6

    def test_straight_ish_loop_has_no_self_intersection(self):
        gates = [
            (20.0, 5.0, 20.0),
            (-20.0, 5.0, 20.0),
            (-20.0, 5.0, -20.0),
            (20.0, 5.0, -20.0),
        ]
        metrics = compute_path_metrics(gates)
        assert metrics["self_intersects"] is False

    def test_pentagram_ordering_self_intersects(self):
        # Visiting 5 points arranged on a circle in "every second point" (pentagram)
        # order is the classic self-intersecting closed polygon -- each edge crosses
        # two others by construction, regardless of the spline smoothing between them.
        n = 5
        radius = 30.0
        circle_points = [
            (radius * math.cos(2 * math.pi * i / n), 5.0, radius * math.sin(2 * math.pi * i / n))
            for i in range(n)
        ]
        star_order = [0, 2, 4, 1, 3]
        gates = [circle_points[i] for i in star_order]
        metrics = compute_path_metrics(gates)
        assert metrics["self_intersects"] is True

    def test_sharp_zigzag_gives_small_turn_radius(self):
        gates = [
            (0.0, 5.0, 0.0),
            (10.0, 5.0, 0.0),
            (10.0, 5.0, 10.0),
            (0.0, 5.0, 10.0),
            (0.0, 5.0, 20.0),
            (10.0, 5.0, 20.0),
        ]
        metrics = compute_path_metrics(gates)
        assert metrics["min_turn_radius_m"] < 15.0

    def test_metrics_are_finite_for_generator_output(self):
        blueprints, checkpoint_ids, spawn_id = generate_procedural_track(seed=42)
        from trackcheck.geometry import geometry_from_blueprints
        geometry = geometry_from_blueprints(blueprints, checkpoint_ids, spawn_id)
        metrics = compute_path_metrics(geometry.gate_positions)
        assert math.isfinite(metrics["total_length_m"])
        assert metrics["total_length_m"] > 0
