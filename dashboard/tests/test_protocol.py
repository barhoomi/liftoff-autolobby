"""Tests for dashboard.control.protocol — the single writer of the plugin's plain-text
protocol files.

The three properties worth guarding are the three reasons the module exists: ownership
enforcement (plugin-owned state can only be reset), atomic replacement (the plugin polls
these files every second and must never see a torn one) and the typed setters producing
byte-exactly what the plugin's parsers expect.
"""

import os

import pytest

from dashboard.control.protocol import (
    READ_ONLY,
    RESET_ONLY,
    WRITABLE,
    ProtocolDir,
    ProtocolOwnershipError,
)


@pytest.fixture
def proto(tmp_path):
    return ProtocolDir(str(tmp_path / "plugins"))


class TestOwnership:
    def test_plugin_owned_files_cannot_be_content_written(self, proto):
        for name in RESET_ONLY:
            with pytest.raises(ProtocolOwnershipError):
                proto.write_text(name, "anything")

    def test_plugin_owned_files_cannot_be_deleted_directly(self, proto):
        with pytest.raises(ProtocolOwnershipError):
            proto.delete("shuffle_order.txt")

    def test_plugin_produced_data_is_read_only(self, proto):
        for name in READ_ONLY:
            with pytest.raises(ProtocolOwnershipError):
                proto.write_text(name, "{}")

    def test_rotation_state_reset_is_the_sanctioned_write(self, proto):
        proto.reset_rotation_state()
        assert proto.read_text("rotation_state.txt") == "0"

    def test_clear_shuffle_order_removes_the_file_and_reports_whether_it_existed(self, proto):
        proto.write_flag("auto_start.txt", True)  # creates the directory
        target = proto.path("shuffle_order.txt")
        with open(target, "w") as f:
            f.write("# signature:deadbeef\n0\n1\n")
        assert proto.clear_shuffle_order() is True
        assert not os.path.exists(target)
        assert proto.clear_shuffle_order() is False  # already gone: silent no-op

    def test_ownership_tables_do_not_overlap(self):
        assert not (set(WRITABLE) & set(RESET_ONLY))
        assert not (set(WRITABLE) & set(READ_ONLY))
        assert not (set(RESET_ONLY) & set(READ_ONLY))


class TestWrites:
    def test_write_creates_the_directory(self, tmp_path):
        proto = ProtocolDir(str(tmp_path / "missing" / "plugins"))
        proto.set_lobby_name("Procedural Loop Room")
        assert proto.read_text("lobby_name.txt") == "Procedural Loop Room"

    def test_write_leaves_no_temp_files_behind(self, proto):
        proto.set_lobby_name("x")
        proto.set_rotation_interval(90)
        leftovers = [n for n in os.listdir(proto.plugins_dir) if ".tmp." in n]
        assert leftovers == []

    def test_write_replaces_rather_than_truncates(self, proto):
        """os.replace swaps a fully written file in. A reader holding the old path sees
        the whole old content, never a zero-length window -- which truncate-then-write
        (the pre-D5 `open(..., "w")`) could expose to the plugin's 1s poll."""
        proto.set_lobby_name("old name")
        old_inode = os.stat(proto.path("lobby_name.txt")).st_ino
        proto.set_lobby_name("new name")
        assert proto.read_text("lobby_name.txt") == "new name"
        assert os.stat(proto.path("lobby_name.txt")).st_ino != old_inode

    def test_lock_is_reentrant_across_sequential_writes(self, proto):
        for i in range(5):
            proto.set_rotation_interval(i)
        assert proto.read_int("rotation_interval.txt") == 4

    def test_unknown_file_names_are_allowed(self, proto):
        # The table gates plugin-owned files; it is not an allowlist, so a protocol file
        # added by a future plugin feature does not need this module edited first.
        proto.write_text("some_future_flag.txt", "true")
        assert proto.read_flag("some_future_flag.txt") is True


class TestTypedSetters:
    def test_flags_use_the_plugins_true_false_wording(self, proto):
        proto.set_auto_start(True)
        proto.set_room_private(False)
        assert proto.read_text("auto_start.txt") == "true"
        assert proto.read_text("room_private.txt") == "false"

    def test_interval_is_written_as_a_bare_integer(self, proto):
        proto.set_rotation_interval(90.7)
        assert proto.read_text("rotation_interval.txt") == "90"

    def test_available_playlists_is_newline_terminated_per_entry(self, proto):
        proto.set_available_playlists(["a", "b"])
        assert proto.read_text("available_playlists.txt") == "a\nb"
        with open(proto.path("available_playlists.txt")) as f:
            assert f.read() == "a\nb\n"

    def test_maintenance_set_creates_and_cancel_deletes(self, proto):
        # The plugin's external-maintenance check is presence-based, and it cancels when
        # the file disappears -- writing "false" would schedule a shutdown, not cancel one.
        assert proto.set_maintenance(True) is True
        assert proto.exists("maintenance_active.txt")
        assert proto.set_maintenance(False) is True
        assert not proto.exists("maintenance_active.txt")
        assert proto.set_maintenance(False) is False

    def test_trigger_skip_now_writes_the_one_shot_file(self, proto):
        # No "cancel" side by design (unlike set_maintenance): the plugin deletes the
        # file itself once HandleGameRoom consumes it, so the control plane's only
        # sanctioned action is to create it.
        proto.trigger_skip_now()
        assert proto.exists("skip_now.txt")
        assert proto.read_text("skip_now.txt") == "true"

    def test_override_game_mode_clears_on_falsy(self, proto):
        proto.set_override_game_mode("Classic Race")
        assert proto.read_text("override_game_mode.txt") == "Classic Race"
        proto.set_override_game_mode(None)
        assert not proto.exists("override_game_mode.txt")


class TestReads:
    def test_missing_file_returns_the_default(self, proto):
        assert proto.read_text("nope.txt") is None
        assert proto.read_text("nope.txt", "fallback") == "fallback"
        assert proto.read_flag("nope.txt") is False
        assert proto.read_flag("nope.txt", True) is True
        assert proto.read_int("nope.txt", 600) == 600

    def test_read_flag_matches_the_plugins_FileFlag_semantics(self, proto):
        # plugin: File.Exists(p) && content.Trim() == "true" (case-insensitive)
        proto.write_text("democracy_mode.txt", "TRUE\n")
        assert proto.read_flag("democracy_mode.txt") is True
        proto.write_text("democracy_mode.txt", "1")
        assert proto.read_flag("democracy_mode.txt") is False

    def test_read_int_tolerates_the_plugins_float_formatting(self, proto):
        # /interval writes newInterval.ToString("F0"), but a hand-edited file may hold
        # "90.0" -- both must read back as 90 rather than raising.
        proto.write_text("rotation_interval.txt", "90.0")
        assert proto.read_int("rotation_interval.txt") == 90
        proto.write_text("rotation_interval.txt", "banana")
        assert proto.read_int("rotation_interval.txt", 600) == 600

    def test_rotation_tracks_are_parsed_skipping_the_header_comment(self, proto):
        proto.write_text(
            "tracks_to_rotate.txt",
            "# Generated from playlist: demo\n"
            "BC Track 0,Bando City,Race\n"
            "Green Track 1,The Green,Infinite Race\n",
        )
        assert proto.read_rotation_tracks() == [
            {"track": "BC Track 0", "environment": "Bando City", "mode": "Race"},
            {"track": "Green Track 1", "environment": "The Green", "mode": "Infinite Race"},
        ]

    def test_rotation_tracks_of_a_missing_file_is_empty(self, proto):
        assert proto.read_rotation_tracks() == []


def test_plugins_dir_is_required():
    with pytest.raises(ValueError):
        ProtocolDir("")
