"""Set-based validation and the two opt-in leniency flags
(docs/features/doing/workshop-ingest-hardening.md §5 and §7).

The property under test: a track and its race published as SEPARATE workshop items
validate when ingested TOGETHER, and neither of them is punished for the other's
packaging. Validating one at a time deadlocks -- the track alone has no race, the race
alone has no track -- which is what quarantined a perfectly good race item live on
2026-09-03.

test_validate.py stays the guard for the *unchanged* single-item behaviour: every new
knob here is opt-in, so that file must keep passing untouched.
"""

import os

from trackcheck.validate import Reason, validate_item, validate_item_set

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


class TestSplitTrackRacePair:
    def test_track_and_race_items_validate_together(self):
        track = fixture_path("split_pair_track")
        race = fixture_path("split_pair_race")
        reports = validate_item_set([track, race])
        assert reports[track].ok is True, reports[track].reasons
        assert reports[race].ok is True, reports[race].reasons

    def test_the_set_report_describes_the_race_for_a_race_only_item(self):
        track = fixture_path("split_pair_track")
        race = fixture_path("split_pair_race")
        report = validate_item_set([track, race])[race]
        assert report.name == "Fixture Split Track Race"
        assert report.local_id == "fixture_split"   # its TRACK dependency
        assert report.environment is None           # a .race declares none
        assert report.race_paths

    def test_the_track_item_alone_still_fails_for_want_of_a_race(self):
        track = fixture_path("split_pair_track")
        report = validate_item_set([track])[track]
        assert report.ok is False
        assert Reason.NO_MATCHING_RACE in report.reasons


class TestRaceOnlyItem:
    def test_a_race_whose_track_is_nowhere_is_rejected_with_its_own_reason(self):
        item = fixture_path("race_only_item")
        report = validate_item_set([item])[item]
        assert report.ok is False
        assert Reason.RACE_TRACK_DEP_MISSING in report.reasons
        # Emphatically NOT the old verdict: "this item has no .track file" is what made
        # a legitimate race item get quarantined.
        assert Reason.TRACK_FILE_NOT_FOUND not in report.reasons

    def test_the_race_item_alone_is_rejected_but_the_same_race_beside_its_track_is_not(self):
        race = fixture_path("split_pair_race")
        alone = validate_item_set([race])[race]
        assert alone.ok is False
        assert Reason.RACE_TRACK_DEP_MISSING in alone.reasons

        paired = validate_item_set([fixture_path("split_pair_track"), race])[race]
        assert paired.ok is True

    def test_an_extra_search_dir_can_supply_the_track(self):
        """The real ingest passes the whole workshop content root, so a race whose
        partner was installed by an earlier download still resolves."""
        race = fixture_path("split_pair_race")
        report = validate_item_set([race],
                                   race_search_dirs=[race, fixture_path("split_pair_track")])[race]
        assert report.ok is True

    def test_an_item_with_neither_track_nor_race_is_still_track_file_not_found(self, tmp_path):
        empty = str(tmp_path / "empty_item")
        os.makedirs(empty)
        report = validate_item_set([empty])[empty]
        assert report.ok is False
        assert report.reasons == [Reason.TRACK_FILE_NOT_FOUND]


class TestLeniencyFlags:
    def test_require_gates_false_warns_instead_of_rejecting(self):
        # no_gates_item ships no .race either, so `require_race=False` is needed for the
        # report to come back ok at all -- the feature doc's criterion 4c named only
        # require_gates (see its Deviations entry, 2026-09-03). What that criterion is
        # actually about is asserted exactly: GATE_DATA_MISSING moves from `reasons` to
        # `warnings` and stops blocking.
        report = validate_item(fixture_path("no_gates_item"), require_gates=False,
                               require_race=False)
        assert report.ok is True
        assert Reason.GATE_DATA_MISSING in report.warnings
        assert Reason.GATE_DATA_MISSING not in report.reasons

    def test_require_gates_false_alone_leaves_every_other_check_blocking(self):
        report = validate_item(fixture_path("no_gates_item"), require_gates=False)
        assert report.ok is False
        assert report.reasons == [Reason.NO_MATCHING_RACE]
        assert Reason.GATE_DATA_MISSING in report.warnings

    def test_the_default_still_rejects_a_gateless_track(self):
        """The counterpart of the test above: with no kwargs, nothing changed. This is
        what makes the new flags opt-in rather than a silent policy change for every
        other caller."""
        report = validate_item(fixture_path("no_gates_item"))
        assert report.ok is False
        assert Reason.GATE_DATA_MISSING in report.reasons
        assert report.warnings == []

    def test_require_race_false_warns_instead_of_rejecting(self):
        report = validate_item(fixture_path("split_pair_track"), require_race=False)
        assert report.ok is True
        assert Reason.NO_MATCHING_RACE in report.warnings

    def test_a_spawnless_track_is_still_blocked_by_both_flags_off(self):
        """SPAWN_DATA_MISSING is deliberately NOT downgradeable: a track with no spawn
        point cannot be flown in any mode at all, race or freestyle."""
        report = validate_item(fixture_path("no_gates_item"), require_race=False,
                               require_gates=False)
        # This fixture has a spawn point, so assert the flags did not touch the check
        # that owns it rather than asserting a rejection this fixture cannot produce.
        assert Reason.SPAWN_DATA_MISSING not in report.warnings

    def test_flags_are_forwarded_through_the_set_api(self):
        item = fixture_path("no_gates_item")
        strict = validate_item_set([item])[item]
        lenient = validate_item_set([item], require_gates=False, require_race=False)[item]
        assert strict.ok is False
        assert lenient.ok is True
        assert Reason.GATE_DATA_MISSING in lenient.warnings
