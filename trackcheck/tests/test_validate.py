import os

from trackcheck.validate import Reason, validate_item

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


class TestValidateItemGoodCase:
    def test_good_item_passes_with_no_reasons(self):
        report = validate_item(fixture_path("good_item"))
        assert report.ok is True
        assert report.reasons == []

    def test_good_item_reports_parsed_fields(self):
        report = validate_item(fixture_path("good_item"))
        assert report.local_id == "fixture_good"
        assert report.name == "Fixture Good Track"
        assert report.environment == "The Drawing Board"
        assert report.race_paths  # matched at least one race


class TestValidateItemRejections:
    def test_no_track_file_at_all(self):
        report = validate_item(fixture_path("master_for_lint_dir_does_not_exist"))
        assert report.ok is False
        assert Reason.TRACK_FILE_NOT_FOUND in report.reasons

    def test_malformed_xml_is_unparseable(self):
        report = validate_item(fixture_path("malformed_xml_item"))
        assert report.ok is False
        assert report.reasons == [Reason.TRACK_FILE_UNPARSEABLE]

    def test_unsupported_environment(self):
        report = validate_item(fixture_path("unsupported_env_item"))
        assert report.ok is False
        assert Reason.ENVIRONMENT_UNSUPPORTED in report.reasons

    def test_unsafe_markup_in_name(self):
        report = validate_item(fixture_path("unsafe_name_item"))
        assert report.ok is False
        assert Reason.NAME_UNSAFE_MARKUP in report.reasons

    def test_no_matching_race(self):
        report = validate_item(fixture_path("no_race_item"))
        assert report.ok is False
        assert Reason.NO_MATCHING_RACE in report.reasons

    def test_no_gates_present(self):
        report = validate_item(fixture_path("no_gates_item"))
        assert report.ok is False
        assert Reason.GATE_DATA_MISSING in report.reasons
        # A spawn point *is* present in this fixture -- only the gate check should fire.
        assert Reason.SPAWN_DATA_MISSING not in report.reasons

    def test_missing_local_id(self):
        report = validate_item(fixture_path("missing_local_id_item"))
        assert report.ok is False
        assert Reason.LOCAL_ID_MISSING in report.reasons
        # No localID means the race-matching check can't run at all -- it should not
        # also produce a spurious NO_MATCHING_RACE on top.
        assert Reason.NO_MATCHING_RACE not in report.reasons


class TestValidateItemRaceSearchDirs:
    def test_race_in_separate_directory_is_found_when_search_dir_passed(self):
        # no_race_item's track is the same content/localID as good_item's, but its
        # matching race lives in good_item/ -- simulates the real Liftoff layout
        # where custom tracks/races are siblings under separate Tracks/ and Races/
        # roots rather than co-located per item.
        report = validate_item(fixture_path("no_race_item"), race_search_dirs=[fixture_path("good_item")])
        assert report.ok is True

    def test_default_race_search_dir_is_item_dir_itself(self):
        report = validate_item(fixture_path("no_race_item"))
        assert Reason.NO_MATCHING_RACE in report.reasons
