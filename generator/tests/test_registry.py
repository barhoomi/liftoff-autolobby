"""Unit tests for generator/src/registry.py -- published_tracks.json."""

import json

import pytest

from trackcheck.geometry import TrackGeometry

from src.registry import (
    append_entry,
    compute_content_hash,
    find_by_content_hash,
    load_registry,
    make_entry,
    save_registry,
)


def _geometry(offset=0.0):
    return TrackGeometry(
        gate_positions=[
            (0.0 + offset, 5.0, 0.0),
            (10.0 + offset, 5.0, 0.0),
            (10.0 + offset, 5.0, 10.0),
            (0.0 + offset, 5.0, 10.0),
        ],
        spawn_position=(0.0 + offset, 0.1, -8.0),
    )


class TestLoadRegistry:
    def test_missing_file_returns_empty_well_formed_registry(self, tmp_path):
        registry = load_registry(str(tmp_path / "does_not_exist.json"))
        assert registry == {"version": 1, "tracks": []}

    def test_loads_existing_file(self, tmp_path):
        path = tmp_path / "published_tracks.json"
        path.write_text(json.dumps({"version": 1, "tracks": [{"workshop_id": "1"}]}))
        registry = load_registry(str(path))
        assert registry["tracks"] == [{"workshop_id": "1"}]

    def test_tolerates_missing_keys_in_existing_file(self, tmp_path):
        path = tmp_path / "published_tracks.json"
        path.write_text(json.dumps({}))
        registry = load_registry(str(path))
        assert registry["version"] == 1
        assert registry["tracks"] == []


class TestSaveRegistry:
    def test_round_trips(self, tmp_path):
        path = tmp_path / "published_tracks.json"
        registry = {"version": 1, "tracks": [{"workshop_id": "42"}]}
        save_registry(registry, str(path))
        reloaded = load_registry(str(path))
        assert reloaded == registry

    def test_writes_pretty_printed_json_with_trailing_newline(self, tmp_path):
        path = tmp_path / "published_tracks.json"
        save_registry({"version": 1, "tracks": []}, str(path))
        text = path.read_text()
        assert text.endswith("\n")
        assert "\n" in text  # pretty-printed, not a single line


class TestComputeContentHash:
    def test_same_geometry_yields_same_hash(self):
        assert compute_content_hash(_geometry()) == compute_content_hash(_geometry())

    def test_different_geometry_yields_different_hash(self):
        assert compute_content_hash(_geometry(0.0)) != compute_content_hash(_geometry(5.0))

    def test_is_a_hex_sha256_digest(self):
        digest = compute_content_hash(_geometry())
        assert len(digest) == 64
        int(digest, 16)  # raises ValueError if not valid hex

    def test_tiny_float_noise_below_rounding_still_matches(self):
        # Simulates float representation drift (e.g. a numpy version bump)
        # that shouldn't change identity of a physically-the-same track.
        a = TrackGeometry(gate_positions=[(0.0, 5.0, 0.0), (10.0, 5.0, 0.0),
                                           (10.0, 5.0, 10.0), (0.0, 5.0, 10.0)],
                           spawn_position=(0.0, 0.1, -8.0))
        b = TrackGeometry(gate_positions=[(0.0000001, 5.0, 0.0), (10.0, 5.0, 0.0),
                                           (10.0, 5.0, 10.0), (0.0, 5.0, 10.0)],
                           spawn_position=(0.0, 0.1, -8.0))
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_handles_no_spawn_position(self):
        geometry = TrackGeometry(gate_positions=[(0.0, 5.0, 0.0)], spawn_position=None)
        # Should not raise.
        compute_content_hash(geometry)


class TestFindByContentHash:
    def test_returns_none_when_absent(self):
        registry = {"version": 1, "tracks": []}
        assert find_by_content_hash(registry, "deadbeef") is None

    def test_finds_matching_entry(self):
        registry = {"version": 1, "tracks": [{"content_hash": "abc123", "workshop_id": "7"}]}
        found = find_by_content_hash(registry, "abc123")
        assert found is not None
        assert found["workshop_id"] == "7"


class TestMakeEntryAndAppend:
    def test_make_entry_has_expected_fields(self):
        entry = make_entry(
            workshop_id="123", track_id="proc_batch_1", name="Procedural Batch 1",
            seed=42, content_hash="abc", tags={"difficulty": "easy", "style": "flow"},
            environment="TheDrawingBoard", published_at="2026-07-17T00:00:00+00:00",
        )
        assert entry == {
            "workshop_id": "123",
            "track_id": "proc_batch_1",
            "name": "Procedural Batch 1",
            "seed": 42,
            "content_hash": "abc",
            "tags": {"difficulty": "easy", "style": "flow"},
            "environment": "TheDrawingBoard",
            "published_at": "2026-07-17T00:00:00+00:00",
        }

    def test_append_entry_mutates_and_returns_registry(self):
        registry = {"version": 1, "tracks": []}
        entry = make_entry(
            workshop_id="1", track_id="t", name="T", seed=1, content_hash="h",
            tags={}, environment="TheDrawingBoard", published_at="now",
        )
        result = append_entry(registry, entry)
        assert result is registry
        assert registry["tracks"] == [entry]

    def test_append_entry_on_registry_missing_tracks_key(self):
        registry = {"version": 1}
        entry = {"workshop_id": "1"}
        append_entry(registry, entry)
        assert registry["tracks"] == [entry]

    def test_idempotence_of_load_append_save_load_cycle(self, tmp_path):
        path = tmp_path / "published_tracks.json"
        registry = load_registry(str(path))
        entry = make_entry(
            workshop_id="1", track_id="t", name="T", seed=1, content_hash="h",
            tags={"difficulty": "easy", "style": "flow"}, environment="TheDrawingBoard",
            published_at="now",
        )
        append_entry(registry, entry)
        save_registry(registry, str(path))

        reloaded = load_registry(str(path))
        assert reloaded["tracks"] == [entry]
        # Loading again without appending must not duplicate anything.
        reloaded_again = load_registry(str(path))
        assert reloaded_again["tracks"] == [entry]
