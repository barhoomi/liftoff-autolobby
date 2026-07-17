"""Unit tests for generator/generate_batch.py -- the full batch pipeline
(generate -> quality_gate -> dedupe by content hash -> classify -> publish ->
registry append).

save_fn and publish_fn are ALWAYS injected fakes here: this suite never writes
under the real Liftoff install and never invokes real steamcmd/Steam. The
required acceptance-criterion test is `TestDedupeOnRerun`, which proves a
second run_batch() call over the same registry does not re-publish content it
has already published (hash-based dedupe).
"""

import json

import pytest

from src.publish import PublishError
from src.registry import load_registry
from generate_batch import PRESETS, run_batch

# PRESETS[0] (circle/8pts/r=45/elevation=4, spacing=18 -- generate_batch's own
# DEFAULT_GATE_SPACING) is the preset trackcheck's calibration measured at
# 0/100 rejections (docs/features/done/track-validation-quality-gate.md), so
# using it with any small fixed seed reliably passes quality_gate -- no flaky
# reliance on random geometry in these tests.
_GUARANTEED_PASS_PRESET = [PRESETS[0]]


def _fixed_seeds(seeds):
    return lambda i: seeds[i]


class _FakeSave:
    """Records calls; never touches the real Liftoff install."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return (f"/fake/{kwargs['track_id']}.track", f"/fake/{kwargs['track_id']}.race")


class _FakePublish:
    """Records calls and returns sequential fake workshop IDs; never touches
    real steamcmd/Steam."""

    def __init__(self, start=1000):
        self.calls = []
        self._next_id = start

    def __call__(self, track_id):
        self.calls.append(track_id)
        result = str(self._next_id)
        self._next_id += 1
        return result


class TestRunBatchPublishesKeepers:
    def test_publishes_all_passing_candidates(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        save_fn = _FakeSave()
        publish_fn = _FakePublish()

        result = run_batch(
            3,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([11, 22, 33]),
            save_fn=save_fn,
            publish_fn=publish_fn,
        )

        assert result.total == 3
        assert len(result.published) == 3
        assert result.rejected == []
        assert result.duplicates == []
        assert result.errors == []
        assert len(save_fn.calls) == 3
        assert len(publish_fn.calls) == 3

    def test_registry_file_is_written_with_expected_fields(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        run_batch(
            1,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([11]),
            save_fn=_FakeSave(),
            publish_fn=_FakePublish(start=555),
        )

        on_disk = load_registry(registry_path)
        assert len(on_disk["tracks"]) == 1
        entry = on_disk["tracks"][0]
        assert entry["workshop_id"] == "555"
        assert entry["seed"] == 11
        assert entry["track_id"] == "proc_batch_1"
        assert "content_hash" in entry and len(entry["content_hash"]) == 64
        assert set(entry["tags"].keys()) == {"difficulty", "style"}
        assert entry["environment"] == "TheDrawingBoard"
        assert "published_at" in entry

    def test_environment_is_passed_through_to_save_and_registry(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        save_fn = _FakeSave()
        run_batch(
            1,
            registry_path=registry_path,
            environment="TheGreen",
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([11]),
            save_fn=save_fn,
            publish_fn=_FakePublish(),
        )
        assert save_fn.calls[0]["environment"] == "TheGreen"
        on_disk = load_registry(registry_path)
        assert on_disk["tracks"][0]["environment"] == "TheGreen"


class TestRunBatchQualityGateRejection:
    def test_rejected_candidates_are_not_saved_or_published(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        save_fn = _FakeSave()
        publish_fn = _FakePublish()

        # Force rejection deterministically regardless of geometry.
        result = run_batch(
            3,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([1, 2, 3]),
            thresholds={"min_gates": 9999},
            save_fn=save_fn,
            publish_fn=publish_fn,
        )

        assert len(result.rejected) == 3
        assert result.published == []
        assert save_fn.calls == []
        assert publish_fn.calls == []
        on_disk = load_registry(registry_path)
        assert on_disk["tracks"] == []

    def test_rejection_reasons_are_recorded(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        result = run_batch(
            1,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([1]),
            thresholds={"min_gates": 9999},
            save_fn=_FakeSave(),
            publish_fn=_FakePublish(),
        )
        assert "TOO_FEW_GATES" in result.rejected[0]["reasons"]


class TestDedupeOnRerun:
    """The spec's explicit acceptance criterion: 'Re-running the batch does not
    duplicate uploads (hash check verified by test)'."""

    def test_second_run_over_same_registry_does_not_republish(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        seeds = [111, 222, 333]

        first_publish = _FakePublish(start=2000)
        first_result = run_batch(
            3,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds(seeds),
            save_fn=_FakeSave(),
            publish_fn=first_publish,
        )
        assert len(first_result.published) == 3
        assert len(first_publish.calls) == 3

        # Re-run with the SAME seeds (-> identical geometry -> identical content
        # hash) against the SAME registry file.
        second_save = _FakeSave()
        second_publish = _FakePublish(start=9000)
        second_result = run_batch(
            3,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds(seeds),
            save_fn=second_save,
            publish_fn=second_publish,
        )

        assert second_result.published == []
        assert len(second_result.duplicates) == 3
        assert second_publish.calls == []  # never re-uploaded
        assert second_save.calls == []  # never even re-staged to disk

        # Registry on disk still has exactly 3 entries, not 6.
        on_disk = load_registry(registry_path)
        assert len(on_disk["tracks"]) == 3

    def test_different_seeds_are_not_treated_as_duplicates(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        run_batch(
            1, registry_path=registry_path, presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([111]), save_fn=_FakeSave(), publish_fn=_FakePublish(),
        )
        second_result = run_batch(
            1, registry_path=registry_path, presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([222]), save_fn=_FakeSave(), publish_fn=_FakePublish(),
        )
        assert len(second_result.published) == 1
        assert second_result.duplicates == []


class TestRunBatchDryRun:
    def test_dry_run_never_saves_or_publishes_or_touches_registry(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        save_fn = _FakeSave()
        publish_fn = _FakePublish()

        result = run_batch(
            2,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([11, 22]),
            save_fn=save_fn,
            publish_fn=publish_fn,
            dry_run=True,
        )

        assert save_fn.calls == []
        assert publish_fn.calls == []
        assert result.published == []
        import os
        assert not os.path.exists(registry_path)


class TestRunBatchErrorHandling:
    def test_publish_error_on_one_candidate_does_not_abort_the_batch(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")

        def flaky_publish(track_id):
            if track_id == "proc_batch_2":
                raise PublishError("steamcmd said no")
            return "7777"

        result = run_batch(
            3,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([1, 2, 3]),
            save_fn=_FakeSave(),
            publish_fn=flaky_publish,
        )

        assert len(result.errors) == 1
        assert result.errors[0]["track_id"] == "proc_batch_2"
        assert len(result.published) == 2

    def test_registry_persists_successes_that_happened_before_a_later_failure(self, tmp_path):
        # Crash-resilience: the registry is saved after every successful
        # publish, not just at the end of the batch -- so a later failure must
        # not lose the earlier, already-published entries.
        registry_path = str(tmp_path / "published_tracks.json")

        def flaky_publish(track_id):
            if track_id == "proc_batch_3":
                raise PublishError("boom")
            return f"id-{track_id}"

        run_batch(
            3,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([1, 2, 3]),
            save_fn=_FakeSave(),
            publish_fn=flaky_publish,
        )

        on_disk = load_registry(registry_path)
        assert len(on_disk["tracks"]) == 2
        assert {e["track_id"] for e in on_disk["tracks"]} == {"proc_batch_1", "proc_batch_2"}

    def test_non_publish_exceptions_are_also_captured_as_errors(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")

        def broken_save(**kwargs):
            raise OSError("disk full")

        result = run_batch(
            1,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([1]),
            save_fn=broken_save,
            publish_fn=_FakePublish(),
        )
        assert len(result.errors) == 1
        assert result.published == []


class TestBatchResult:
    def test_total_reflects_generated_count_regardless_of_outcome(self, tmp_path):
        registry_path = str(tmp_path / "published_tracks.json")
        result = run_batch(
            4,
            registry_path=registry_path,
            presets=_GUARANTEED_PASS_PRESET,
            seed_fn=_fixed_seeds([1, 2, 3, 4]),
            thresholds={"min_gates": 9999},  # all rejected
            save_fn=_FakeSave(),
            publish_fn=_FakePublish(),
        )
        assert result.total == 4
        assert len(result.rejected) == 4
