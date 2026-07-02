import pytest

from src.assets import GATE_OCTAGON, SPAWN_SINGLE
from src.generator import generate_procedural_track


def _blueprint_by_id(blueprints, instance_id):
    return next(b for b in blueprints if b["instance_id"] == instance_id)


class TestGenerateProceduralTrack:
    @pytest.mark.parametrize("shape", ["circle", "triangle", "square"])
    def test_generates_without_crashing_for_all_shapes(self, shape):
        blueprints, checkpoint_ids, spawn_point_id = generate_procedural_track(
            seed=1, shape=shape
        )
        assert len(checkpoint_ids) > 0
        assert spawn_point_id is not None

    def test_same_seed_is_deterministic(self):
        result_a = generate_procedural_track(seed=42)
        result_b = generate_procedural_track(seed=42)
        assert result_a == result_b

    def test_different_seeds_produce_different_layouts(self):
        blueprints_a, _, _ = generate_procedural_track(seed=1)
        blueprints_b, _, _ = generate_procedural_track(seed=2)
        assert blueprints_a != blueprints_b

    def test_spawn_point_uses_spawn_asset(self):
        blueprints, _, spawn_point_id = generate_procedural_track(seed=1)
        spawn_bp = _blueprint_by_id(blueprints, spawn_point_id)
        assert spawn_bp["item_id"] == SPAWN_SINGLE

    def test_checkpoints_use_gate_asset(self):
        blueprints, checkpoint_ids, _ = generate_procedural_track(seed=1)
        for cid in checkpoint_ids:
            assert _blueprint_by_id(blueprints, cid)["item_id"] == GATE_OCTAGON

    def test_gates_never_clip_through_ground(self):
        # generator.py enforces pos[1] >= 2.5 so the 5m-tall gate mesh doesn't
        # clip through the ground plane (Y=0) -- regression guard for that invariant.
        blueprints, checkpoint_ids, _ = generate_procedural_track(seed=7)
        for cid in checkpoint_ids:
            gate = _blueprint_by_id(blueprints, cid)
            assert gate["position"][1] >= 2.5

    def test_instance_ids_are_unique(self):
        blueprints, _, _ = generate_procedural_track(seed=3)
        ids = [b["instance_id"] for b in blueprints]
        assert len(ids) == len(set(ids))

    def test_smaller_gate_spacing_yields_more_gates(self):
        _, sparse_ids, _ = generate_procedural_track(seed=5, gate_spacing=40.0)
        _, dense_ids, _ = generate_procedural_track(seed=5, gate_spacing=10.0)
        assert len(dense_ids) > len(sparse_ids)
