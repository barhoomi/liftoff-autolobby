"""Tests for main.extract_track_name_from_rotation_line -- the rightmost-split parser
used by generator/main.py's tracks_to_rotate.txt duplicate-prevention check.

bug-comma-in-track-name.md: a comma inside a real track name (the operator's live
report: "Iceberg, Right ahead!") sheared the old plain split(",")[0]. See
docs/features/doing/bug-comma-in-track-name.md for the rightmost-split decision this
mirrors from the plugin's ParseTrackLine and dashboard.control.protocol.parse_track_line.
"""

from main import extract_track_name_from_rotation_line


class TestExtractTrackNameFromRotationLine:
    def test_plain_no_comma_name(self):
        assert extract_track_name_from_rotation_line("BC Track 0,Bando City,Race") == "BC Track 0"

    def test_operator_reported_track_name_with_a_comma(self):
        assert extract_track_name_from_rotation_line(
            "Iceberg, Right ahead!,Bando City,Race"
        ) == "Iceberg, Right ahead!"

    def test_track_name_with_a_comma_and_a_space_after_it(self):
        assert extract_track_name_from_rotation_line(
            "Iceberg, Right ahead!, Bando City, Race"
        ) == "Iceberg, Right ahead!"

    def test_hypothetical_two_comma_track_name(self):
        assert extract_track_name_from_rotation_line("A, B, C,Bando City,Race") == "A, B, C"

    def test_missing_trailing_fields(self):
        assert extract_track_name_from_rotation_line("SoloName") == "SoloName"

    def test_empty_line(self):
        assert extract_track_name_from_rotation_line("") == ""
