import math

import numpy as np
import pytest

from src.math_utils import catmull_rom_spline, get_euler_rotations


def _square_control_points():
    return np.array([
        [10.0, 5.0, 10.0],
        [-10.0, 5.0, 10.0],
        [-10.0, 5.0, -10.0],
        [10.0, 5.0, -10.0],
    ])


class TestCatmullRomSpline:
    def test_requires_at_least_four_control_points(self):
        with pytest.raises(ValueError):
            catmull_rom_spline(np.array([[0.0, 0.0, 0.0]] * 3))

    def test_closed_spline_point_count(self):
        cp = _square_control_points()
        pts, tangents = catmull_rom_spline(cp, num_points_per_segment=10, closed=True)
        # closed: segments == N control points, so len == N * num_points_per_segment
        assert len(pts) == len(cp) * 10
        assert len(tangents) == len(pts)

    def test_no_nans_or_infs(self):
        cp = _square_control_points()
        pts, tangents = catmull_rom_spline(cp, num_points_per_segment=20, closed=True)
        assert np.isfinite(pts).all()
        assert np.isfinite(tangents).all()

    def test_tangents_are_unit_vectors(self):
        cp = _square_control_points()
        _, tangents = catmull_rom_spline(cp, num_points_per_segment=20, closed=True)
        norms = np.linalg.norm(tangents, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)

    def test_loop_is_continuous(self):
        # A closed loop's last sampled point should be close to the first
        # (they're both near the same control point since it's a full circuit).
        cp = _square_control_points()
        pts, _ = catmull_rom_spline(cp, num_points_per_segment=30, closed=True)
        # Distance between consecutive points should be small and roughly uniform,
        # i.e. no huge jump anywhere in the loop (which would indicate a broken segment).
        diffs = np.diff(np.vstack([pts, pts[0]]), axis=0)
        step_lengths = np.linalg.norm(diffs, axis=1)
        assert step_lengths.max() < 5 * step_lengths.mean()


class TestGetEulerRotations:
    def test_facing_positive_z_has_zero_yaw_and_pitch(self):
        pitch, yaw, roll = get_euler_rotations((0.0, 0.0, 1.0))
        assert yaw == pytest.approx(0.0, abs=1e-6)
        assert pitch == pytest.approx(0.0, abs=1e-6)
        assert roll == 0.0

    def test_facing_positive_x_has_90_degree_yaw(self):
        _, yaw, _ = get_euler_rotations((1.0, 0.0, 0.0))
        assert yaw == pytest.approx(90.0, abs=1e-6)

    def test_yaw_is_normalized_to_0_360(self):
        # -Z direction should wrap to 180, not -180 or 360.
        _, yaw, _ = get_euler_rotations((0.0, 0.0, -1.0))
        assert 0.0 <= yaw < 360.0
        assert yaw == pytest.approx(180.0, abs=1e-6)

    def test_climbing_tangent_gives_negative_pitch(self):
        # Straight up (+Y) should read as a steep climb: negative pitch per the
        # Liftoff convention documented in the function's docstring.
        pitch, _, _ = get_euler_rotations((0.0, 1.0, 0.0))
        assert pitch == pytest.approx(-90.0, abs=1e-6)

    def test_diving_tangent_gives_positive_pitch(self):
        pitch, _, _ = get_euler_rotations((0.0, -1.0, 0.0))
        assert pitch == pytest.approx(90.0, abs=1e-6)

    def test_degenerate_zero_vector_does_not_crash(self):
        pitch, yaw, roll = get_euler_rotations((0.0, 0.0, 0.0))
        assert math.isfinite(pitch)
        assert math.isfinite(yaw)
        assert roll == 0.0

    def test_roll_is_always_zero(self):
        for tangent in [(1.0, 0.0, 0.0), (0.3, 0.6, 0.1), (0.0, -1.0, 0.0)]:
            _, _, roll = get_euler_rotations(tangent)
            assert roll == 0.0
