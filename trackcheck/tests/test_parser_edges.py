"""Edge-case coverage for trackcheck.parser beyond what the on-disk fixtures in
tests/fixtures/ exercise: per-element fail-soft behavior (skipped blueprints/passages,
defaulted coordinates), the localID direct-text fallback, and the XML-declaration
stripping in parse_xml_robust. Inputs are written inline to tmp_path so each test
documents exactly which malformed shape it is guarding against.
"""

from trackcheck.parser import (
    normalize_env,
    parse_race_checkpoints,
    parse_track_blueprints,
    parse_track_file,
    parse_xml_robust,
)


def _write(tmp_path, name, content, encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(content.encode(encoding))
    return str(path)


TRACK_EDGE_XML = """<?xml version="1.0" encoding="utf-8"?>
<Track xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <localID><str>edge</str></localID>
  <name>Edge Track</name>
  <environment>TheDrawingBoard</environment>
  <blueprints>
    <TrackBlueprint xsi:type="TrackBlueprintFlag">
      <itemID>GateNoPosition</itemID>
      <instanceID>7</instanceID>
    </TrackBlueprint>
    <TrackBlueprint xsi:type="TrackBlueprintFlag">
      <itemID>GateNoInstanceId</itemID>
    </TrackBlueprint>
    <TrackBlueprint xsi:type="TrackBlueprintFlag">
      <itemID>GateBadInstanceId</itemID>
      <instanceID>not_a_number</instanceID>
    </TrackBlueprint>
    <TrackBlueprint xsi:type="TrackBlueprintFlag">
      <itemID>GatePartialPosition</itemID>
      <instanceID>9</instanceID>
      <position><x>bogus</x><y>2.5</y></position>
      <purpose>Cosmetic</purpose>
    </TrackBlueprint>
  </blueprints>
</Track>
"""


class TestParseTrackBlueprintsEdges:
    def test_blueprint_without_instance_id_is_skipped(self, tmp_path):
        blueprints = parse_track_blueprints(_write(tmp_path, "edge.track", TRACK_EDGE_XML))
        item_ids = [bp["item_id"] for bp in blueprints]
        assert "GateNoInstanceId" not in item_ids

    def test_blueprint_with_non_integer_instance_id_is_skipped(self, tmp_path):
        blueprints = parse_track_blueprints(_write(tmp_path, "edge.track", TRACK_EDGE_XML))
        item_ids = [bp["item_id"] for bp in blueprints]
        assert "GateBadInstanceId" not in item_ids

    def test_valid_blueprints_survive_the_bad_siblings(self, tmp_path):
        blueprints = parse_track_blueprints(_write(tmp_path, "edge.track", TRACK_EDGE_XML))
        assert [bp["instance_id"] for bp in blueprints] == [7, 9]

    def test_missing_position_defaults_to_origin(self, tmp_path):
        blueprints = parse_track_blueprints(_write(tmp_path, "edge.track", TRACK_EDGE_XML))
        no_pos = next(bp for bp in blueprints if bp["item_id"] == "GateNoPosition")
        assert no_pos["position"] == (0.0, 0.0, 0.0)

    def test_unparseable_or_missing_coordinates_default_to_zero(self, tmp_path):
        # x is non-numeric, z is absent entirely: both fall back to 0.0, y is kept.
        blueprints = parse_track_blueprints(_write(tmp_path, "edge.track", TRACK_EDGE_XML))
        partial = next(bp for bp in blueprints if bp["item_id"] == "GatePartialPosition")
        assert partial["position"] == (0.0, 2.5, 0.0)

    def test_purpose_is_none_when_absent_and_kept_when_present(self, tmp_path):
        blueprints = parse_track_blueprints(_write(tmp_path, "edge.track", TRACK_EDGE_XML))
        by_item = {bp["item_id"]: bp for bp in blueprints}
        assert by_item["GateNoPosition"]["purpose"] is None
        assert by_item["GatePartialPosition"]["purpose"] == "Cosmetic"

    def test_track_with_no_blueprints_returns_empty_list_not_none(self, tmp_path):
        xml = """<?xml version="1.0"?><Track><name>Bare</name></Track>"""
        assert parse_track_blueprints(_write(tmp_path, "bare.track", xml)) == []


RACE_EDGE_XML = """<?xml version="1.0" encoding="utf-8"?>
<Race>
  <name>Edge Race</name>
  <spawnPointID>not_an_int</spawnPointID>
  <checkPointPassages>
    <RaceCheckpointPassage>
      <uniqueId>a</uniqueId>
      <checkPointID>5</checkPointID>
      <passageType>Start</passageType>
    </RaceCheckpointPassage>
    <RaceCheckpointPassage>
      <uniqueId>b</uniqueId>
      <passageType>Pass</passageType>
    </RaceCheckpointPassage>
    <RaceCheckpointPassage>
      <uniqueId>c</uniqueId>
      <checkPointID>garbage</checkPointID>
      <passageType>Pass</passageType>
    </RaceCheckpointPassage>
    <RaceCheckpointPassage>
      <uniqueId>d</uniqueId>
      <checkPointID>5</checkPointID>
      <passageType>Finish</passageType>
    </RaceCheckpointPassage>
  </checkPointPassages>
</Race>
"""


class TestParseRaceCheckpointsEdges:
    def test_non_integer_spawn_point_id_becomes_none(self, tmp_path):
        info = parse_race_checkpoints(_write(tmp_path, "edge.race", RACE_EDGE_XML))
        assert info["spawn_point_id"] is None

    def test_missing_spawn_point_element_becomes_none(self, tmp_path):
        xml = """<?xml version="1.0"?><Race><name>NoSpawn</name></Race>"""
        info = parse_race_checkpoints(_write(tmp_path, "nospawn.race", xml))
        assert info["spawn_point_id"] is None

    def test_passages_without_valid_checkpoint_ids_are_skipped(self, tmp_path):
        # The missing-checkPointID and non-integer passages drop out; the valid
        # Start/Finish pair survives in document order.
        info = parse_race_checkpoints(_write(tmp_path, "edge.race", RACE_EDGE_XML))
        assert info["checkpoint_sequence"] == [5, 5]

    def test_race_with_no_passages_yields_empty_sequence(self, tmp_path):
        xml = """<?xml version="1.0"?><Race><name>Empty</name><spawnPointID>3</spawnPointID></Race>"""
        info = parse_race_checkpoints(_write(tmp_path, "empty.race", xml))
        assert info == {"spawn_point_id": 3, "checkpoint_sequence": []}


class TestParseTrackFileLocalIdFallback:
    def test_local_id_direct_text_without_str_child(self, tmp_path):
        # Some files carry the id as <localID>text</localID> with no <str> wrapper;
        # parse_track_file falls back to the element's own text.
        xml = """<?xml version="1.0"?>
<Track>
  <localID>direct_id</localID>
  <name>Direct</name>
  <environment>TheGreen</environment>
</Track>"""
        result = parse_track_file(_write(tmp_path, "direct.track", xml))
        assert result == ("direct_id", "Direct", "TheGreen")

    def test_whitespace_around_fields_is_stripped(self, tmp_path):
        xml = """<?xml version="1.0"?>
<Track>
  <localID><str>  padded  </str></localID>
  <name>  Padded Name  </name>
  <environment>  TheGreen  </environment>
</Track>"""
        result = parse_track_file(_write(tmp_path, "padded.track", xml))
        assert result == ("padded", "Padded Name", "TheGreen")


class TestParseXmlRobustDeclarationStripping:
    def test_lying_encoding_declaration_is_stripped(self, tmp_path):
        # A utf-8 file whose declaration *claims* utf-16. Without stripping the
        # declaration, ElementTree refuses to parse a str carrying an encoding decl.
        xml = '<?xml version="1.0" encoding="utf-16"?><Track><name>Liar</name></Track>'
        root = parse_xml_robust(_write(tmp_path, "liar.track", xml))
        assert root.find("name").text == "Liar"

    def test_uppercase_declaration_is_also_stripped(self, tmp_path):
        xml = '<?XML VERSION="1.0" ENCODING="UTF-8"?><Track><name>Shouty</name></Track>'
        root = parse_xml_robust(_write(tmp_path, "shouty.track", xml))
        assert root.find("name").text == "Shouty"


class TestNormalizeEnvFallbackShapes:
    def test_single_lowercase_word_passes_through_unchanged(self):
        assert normalize_env("weirdplace") == "weirdplace"

    def test_leading_capital_single_word_unchanged(self):
        assert normalize_env("Surtur") == "Surtur"

    def test_consecutive_capitals_split_between_each(self):
        # Documents (not endorses) the regex fallback's behavior on acronyms:
        # every capital after the first gets its own split.
        assert normalize_env("ABCTrack") == "A B C Track"
