"""Tests for dashboard.control.state — the JSONL-events + protocol-files snapshot.

The derivation rules worth pinning down are the ones where being wrong would put a
confident lie on the operator's screen: a stale player roster after a disconnect, a
countdown that keeps ticking while rotation is paused, and (bug-comma-in-track-name.md,
Bug 3) a "next track"/rotation-list guess while shuffle mode is on but no trustworthy
persisted shuffle_order.txt deal exists yet to reflect (this process still must not
fabricate one -- see dashboard.control.shuffle_order).
"""

import os
from datetime import datetime, timezone

from dashboard.control import shuffle_order as shuffle_order_mod
from dashboard.control import state as state_mod
from dashboard.control.protocol import ProtocolDir

NOW = datetime(2026, 8, 5, 12, 10, 0, tzinfo=timezone.utc)


def ev(name, ts="2026-08-05T12:00:00Z", source="plugin", **fields):
    record = {"ts": ts, "source": source, "event": name}
    record.update(fields)
    return record


class TestParseTs:
    def test_parses_the_schema_format_as_utc(self):
        assert state_mod.parse_ts("2026-08-05T12:00:00Z") == \
            datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_rejects_anything_else_without_raising(self):
        assert state_mod.parse_ts("not a time") is None
        assert state_mod.parse_ts(None) is None
        assert state_mod.parse_ts(12345) is None


class TestFoldEvents:
    def test_empty_history_yields_all_unknowns(self):
        live = state_mod.fold_events([], now=NOW)
        assert live["current_track"] is None
        assert live["players"] == []
        assert live["uptime_s"] is None

    def test_latest_rotation_wins(self):
        live = state_mod.fold_events([
            ev("rotation", track="A", env="Bando City", index=0),
            ev("rotation", track="B", env="The Green", mode="Race", index=1),
        ], now=NOW)
        assert live["current_track"] == "B"
        assert live["current_environment"] == "The Green"
        assert live["rotation_index"] == 1

    def test_joins_and_leaves_build_a_roster(self):
        live = state_mod.fold_events([
            ev("player_join", player="alice", userId="1", count=2),
            ev("player_join", player="bob", userId="2", count=3),
            ev("player_leave", player="alice", userId="1", count=2),
        ], now=NOW)
        assert [p["player"] for p in live["players"]] == ["bob"]
        assert live["player_count"] == 2

    def test_a_disconnect_clears_the_roster(self):
        # Without this the dashboard shows ghosts forever: nobody emits player_leave for
        # players who were in a room the bot itself dropped out of.
        live = state_mod.fold_events([
            ev("player_join", player="alice", userId="1"),
            ev("disconnect", cause="OnDisconnected", elapsed_s=291.0),
        ], now=NOW)
        assert live["players"] == []
        assert live["last_disconnect"]["cause"] == "OnDisconnected"

    def test_a_game_restart_clears_roster_and_current_track(self):
        live = state_mod.fold_events([
            ev("rotation", track="A"),
            ev("player_join", player="alice", userId="1"),
            ev("game_start", source="orchestrator", pid=4242),
        ], now=NOW)
        assert live["players"] == []
        assert live["current_track"] is None
        assert live["game_pid"] == 4242

    def test_players_without_identity_are_ignored(self):
        # The plugin already drops identity-less Photon dispatches, but a hand-edited or
        # older log can still contain one; it must not create a blank roster row.
        live = state_mod.fold_events([ev("player_join", count=2)], now=NOW)
        assert live["players"] == []

    def test_uptime_comes_from_the_orchestrator_start(self):
        live = state_mod.fold_events(
            [ev("orchestrator_start", ts="2026-08-05T12:00:00Z", source="orchestrator", interval=90)],
            now=NOW)
        assert live["uptime_s"] == 600.0

    def test_fresh_room_entry_restarts_the_rotation_clock(self):
        live = state_mod.fold_events([
            ev("rotation", ts="2026-08-05T11:00:00Z", track="A"),
            ev("room_entered", ts="2026-08-05T12:05:00Z", fresh=True),
        ], now=NOW)
        assert live["rotation_started_ts"] == "2026-08-05T12:05:00Z"

    def test_settings_update_room_entry_does_not_restart_the_clock(self):
        live = state_mod.fold_events([
            ev("rotation", ts="2026-08-05T12:00:00Z", track="A"),
            ev("room_entered", ts="2026-08-05T12:05:00Z", fresh=False),
        ], now=NOW)
        assert live["rotation_started_ts"] == "2026-08-05T12:00:00Z"

    def test_errors_are_collected_and_capped(self):
        history = [ev("error", message=f"boom {i}", context="playlist_resolution")
                   for i in range(state_mod.MAX_RECENT_ERRORS + 5)]
        live = state_mod.fold_events(history, now=NOW)
        assert len(live["recent_errors"]) == state_mod.MAX_RECENT_ERRORS
        assert live["recent_errors"][-1]["message"].endswith("14")


class TestSnapshot:
    def _protocol(self, tmp_path, **files):
        proto = ProtocolDir(str(tmp_path / "plugins"))
        for name, value in files.items():
            proto.write_text(name.replace("__", "."), value)
        return proto

    def test_config_half_reads_the_protocol_files(self, tmp_path):
        proto = self._protocol(
            tmp_path,
            lobby_name__txt="Procedural Loop Room",
            playlist_name__txt="all_official_races",
            rotation_interval__txt="90",
            room_private__txt="false",
            auto_start__txt="true",
            tracks_to_rotate__txt="# header\nA,Bando City,Race\nB,The Green,Race\n",
        )
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        cfg = snapshot["config"]
        assert cfg["lobby_name"] == "Procedural Loop Room"
        assert cfg["room_private"] is False
        assert cfg["auto_start"] is True
        assert cfg["rotation_interval_s"] == 90
        assert cfg["track_count"] == 2

    def test_countdown_is_interval_minus_elapsed(self, tmp_path):
        proto = self._protocol(tmp_path, rotation_interval__txt="900")
        snapshot = state_mod.build_snapshot(
            proto, None, now=NOW,
            event_list=[ev("rotation", ts="2026-08-05T12:05:00Z", track="A")])
        assert snapshot["rotation"]["elapsed_s"] == 300.0
        assert snapshot["rotation"]["remaining_s"] == 600.0

    def test_countdown_never_goes_negative(self, tmp_path):
        proto = self._protocol(tmp_path, rotation_interval__txt="60")
        snapshot = state_mod.build_snapshot(
            proto, None, now=NOW,
            event_list=[ev("rotation", ts="2026-08-05T12:00:00Z", track="A")])
        assert snapshot["rotation"]["remaining_s"] == 0.0

    def test_paused_rotation_reports_no_countdown(self, tmp_path):
        proto = self._protocol(tmp_path, rotation_interval__txt="900",
                               rotation_paused__txt="true")
        snapshot = state_mod.build_snapshot(
            proto, None, now=NOW,
            event_list=[ev("rotation", ts="2026-08-05T12:05:00Z", track="A")])
        assert snapshot["rotation"]["remaining_s"] is None
        assert snapshot["rotation"]["paused"] is True

    def test_disengaged_rotation_reports_no_countdown(self, tmp_path):
        proto = self._protocol(tmp_path, rotation_interval__txt="900",
                               rotation_engaged__txt="false")
        snapshot = state_mod.build_snapshot(
            proto, None, now=NOW,
            event_list=[ev("rotation", ts="2026-08-05T12:05:00Z", track="A")])
        assert snapshot["rotation"]["engaged"] is False
        assert snapshot["rotation"]["remaining_s"] is None

    def test_rotation_engaged_defaults_to_true_when_the_file_is_absent(self, tmp_path):
        proto = self._protocol(tmp_path, rotation_interval__txt="900")
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        assert snapshot["config"]["rotation_engaged"] is True
        assert snapshot["config"]["rotation_paused"] is False

    def test_next_track_follows_the_cursor_when_shuffle_is_off(self, tmp_path):
        proto = self._protocol(
            tmp_path,
            tracks_to_rotate__txt="A,Bando City,Race\nB,The Green,Race\nC,Hannover,Race\n")
        proto.write_text("shuffle_mode.txt", "false")
        proto.reset_rotation_state()
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        assert snapshot["rotation"]["next_track"]["track"] == "A"

    def test_next_track_is_withheld_while_shuffling_with_no_persisted_deal(self, tmp_path):
        # No shuffle_order.txt on disk yet -- guessing the next track from the static
        # file would be a second, wrong copy of rotation order (AGENTS.md rule 4).
        proto = self._protocol(tmp_path, shuffle_mode__txt="true",
                               tracks_to_rotate__txt="A,Bando City,Race\nB,The Green,Race\n")
        proto.reset_rotation_state()
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        assert snapshot["rotation"]["next_track"] is None
        assert snapshot["config"]["shuffled"] is False

    def _write_shuffle_order(self, proto, static_lines, order):
        sig = shuffle_order_mod.compute_tracks_signature(static_lines)
        content = "# signature:{}\n".format(sig) + "\n".join(str(i) for i in order) + "\n"
        os.makedirs(proto.plugins_dir, exist_ok=True)
        with open(proto.path("shuffle_order.txt"), "w", encoding="utf-8") as f:
            f.write(content)

    def test_tracks_and_next_track_follow_the_persisted_shuffle_order(self, tmp_path):
        """bug-comma-in-track-name.md, Bug 3: with a trustworthy shuffle_order.txt on
        disk, the rotation panel must show the ACTUAL play order, not file order."""
        proto = self._protocol(
            tmp_path, shuffle_mode__txt="true",
            tracks_to_rotate__txt="A,Bando City,Race\nB,The Green,Race\nC,Hannover,Race\n")
        self._write_shuffle_order(
            proto, ["A,Bando City,Race", "B,The Green,Race", "C,Hannover,Race"], [2, 0, 1])
        proto.reset_rotation_state()
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        cfg = snapshot["config"]
        assert cfg["shuffled"] is True
        assert [t["track"] for t in cfg["tracks"]] == ["C", "A", "B"]
        assert snapshot["rotation"]["next_track"]["track"] == "C"

    def test_stale_shuffle_order_falls_back_to_file_order(self, tmp_path):
        proto = self._protocol(
            tmp_path, shuffle_mode__txt="true",
            tracks_to_rotate__txt="A,Bando City,Race\nB,The Green,Race\n")
        # Dealt against different (older) content -- signature won't match.
        self._write_shuffle_order(proto, ["A,Bando City,Race"], [0])
        proto.reset_rotation_state()
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        cfg = snapshot["config"]
        assert cfg["shuffled"] is False
        assert [t["track"] for t in cfg["tracks"]] == ["A", "B"]
        assert snapshot["rotation"]["next_track"] is None

    def test_snapshot_survives_a_completely_empty_plugins_dir(self, tmp_path):
        proto = ProtocolDir(str(tmp_path / "empty"))
        snapshot = state_mod.build_snapshot(proto, None, event_list=[], now=NOW)
        assert snapshot["config"]["track_count"] == 0
        assert snapshot["rotation"]["remaining_s"] is None
        assert snapshot["generated_ts"] == "2026-08-05T12:10:00Z"
