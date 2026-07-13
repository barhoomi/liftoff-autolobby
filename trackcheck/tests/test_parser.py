import os

import pytest

from trackcheck.parser import (
    ENV_MAPPING,
    is_known_environment,
    normalize_env,
    parse_race_checkpoints,
    parse_race_file,
    parse_track_blueprints,
    parse_track_file,
    parse_xml_robust,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


class TestNormalizeEnv:
    def test_known_mapping(self):
        assert normalize_env("TheDrawingBoard") == "The Drawing Board"
        assert normalize_env("BardwellsYard") == "Bardwell's Yard"

    def test_already_normalized_name_passes_through(self):
        assert normalize_env("The Drawing Board") == "The Drawing Board"

    def test_unmapped_camel_case_falls_back_to_spaced_words(self):
        assert normalize_env("SomeBrandNewEnvironment") == "Some Brand New Environment"

    def test_empty_or_none_is_unknown(self):
        assert normalize_env(None) == "Unknown"
        assert normalize_env("") == "Unknown"


class TestIsKnownEnvironment:
    def test_known_camel_case_key(self):
        assert is_known_environment("TheDrawingBoard") is True

    def test_known_spaced_key(self):
        assert is_known_environment("The Green") is True

    def test_unknown_environment_is_false_even_though_normalize_env_would_guess(self):
        # normalize_env() would happily spaced-split this into "Totally Made Up Planet",
        # but it's not a real Liftoff environment -- is_known_environment must say so.
        assert is_known_environment("TotallyMadeUpPlanet") is False

    def test_empty_or_none_is_false(self):
        assert is_known_environment(None) is False
        assert is_known_environment("") is False

    def test_every_env_mapping_key_is_known(self):
        for key in ENV_MAPPING:
            assert is_known_environment(key) is True


class TestParseXmlRobust:
    def test_utf16_encoded_file_falls_back_correctly(self):
        root = parse_xml_robust(fixture_path("utf16_item", "fixture_utf16.track"))
        assert root.find("name").text == "UTF16 Encoded Track"

    def test_malformed_xml_raises(self):
        with pytest.raises(Exception):
            parse_xml_robust(fixture_path("malformed_xml_item", "fixture_malformed.track"))


class TestParseTrackFile:
    def test_good_track_parses_all_fields(self):
        result = parse_track_file(fixture_path("good_item", "fixture_good.track"))
        assert result == ("fixture_good", "Fixture Good Track", "TheDrawingBoard")

    def test_utf16_track_parses(self):
        result = parse_track_file(fixture_path("utf16_item", "fixture_utf16.track"))
        assert result == ("fixture_utf16", "UTF16 Encoded Track", "TheGreen")

    def test_name_with_escaped_markup_round_trips_as_literal_tags(self):
        # XML-escaped &lt;b&gt; in the source decodes back to a literal "<b>" in
        # the parsed .text -- this is exactly the case validate.py's
        # NAME_UNSAFE_MARKUP check needs to catch.
        result = parse_track_file(fixture_path("unsafe_name_item", "fixture_unsafe.track"))
        assert result == ("fixture_unsafe", "<b>Fixture</b> Unsafe Track", "TheDrawingBoard")

    def test_malformed_xml_returns_none(self):
        assert parse_track_file(fixture_path("malformed_xml_item", "fixture_malformed.track")) is None

    def test_missing_local_id_returns_none_for_that_field(self):
        local_id, name, env = parse_track_file(fixture_path("missing_local_id_item", "fixture_missing_id.track"))
        assert local_id is None
        assert name == "Fixture Good Track"

    def test_nonexistent_file_returns_none(self):
        assert parse_track_file(fixture_path("good_item", "does_not_exist.track")) is None


class TestParseRaceFile:
    def test_good_race_parses_track_dependency(self):
        name, track_dep = parse_race_file(fixture_path("good_item", "fixture_good_race_0001.race"))
        assert track_dep == "fixture_good"
        assert name == "Fixture Good Track Race"

    def test_nonexistent_file_returns_none(self):
        assert parse_race_file(fixture_path("good_item", "does_not_exist.race")) is None


class TestParseTrackBlueprints:
    def test_good_track_has_gate_and_spawn_blueprints(self):
        blueprints = parse_track_blueprints(fixture_path("good_item", "fixture_good.track"))
        item_ids = {bp["item_id"] for bp in blueprints}
        assert any("Gate" in iid for iid in item_ids if iid)
        assert any("SpawnPoint" in iid for iid in item_ids if iid)

    def test_positions_are_3_tuples_of_floats(self):
        blueprints = parse_track_blueprints(fixture_path("good_item", "fixture_good.track"))
        for bp in blueprints:
            assert len(bp["position"]) == 3
            assert all(isinstance(v, float) for v in bp["position"])

    def test_no_gates_item_has_no_gate_blueprints(self):
        blueprints = parse_track_blueprints(fixture_path("no_gates_item", "fixture_nogates.track"))
        item_ids = [bp["item_id"] for bp in blueprints if bp["item_id"]]
        assert not any("Gate" in iid or "Checkpoint" in iid for iid in item_ids)

    def test_malformed_xml_returns_none(self):
        assert parse_track_blueprints(fixture_path("malformed_xml_item", "fixture_malformed.track")) is None


class TestParseRaceCheckpoints:
    def test_good_race_returns_spawn_and_sequence(self):
        info = parse_race_checkpoints(fixture_path("good_item", "fixture_good_race_0001.race"))
        assert info["spawn_point_id"] == 1
        # 5 gates + the repeated Finish-back-to-start passage == 6 entries
        assert len(info["checkpoint_sequence"]) == 6
        assert info["checkpoint_sequence"][0] == info["checkpoint_sequence"][-1]

    def test_malformed_xml_returns_none(self):
        assert parse_race_checkpoints(fixture_path("malformed_xml_item", "fixture_malformed.track")) is None
