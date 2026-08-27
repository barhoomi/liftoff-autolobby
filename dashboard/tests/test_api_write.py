"""Tests for the dashboard's write-side HTTP API (playlist CRUD + bot controls).

Every assertion checks the *file* the plugin would read, not just the response body —
these endpoints exist to move bytes into the protocol directory, and a 200 that wrote
nothing is precisely the "claims success, does nothing" shape AGENTS.md rule 2 warns
about.
"""

import json


def entry(env="Bando City", track="*", mode="Classic Race"):
    return {"environment": env, "track": track, "mode": mode}


class TestAuth:
    def test_write_routes_reject_a_missing_token(self, anon_client):
        assert anon_client.post("/api/control/interval", json={"seconds": 90}).status_code == 401
        assert anon_client.put("/api/playlists/x", json={"items": []}).status_code == 401
        assert anon_client.get("/api/playlists").status_code == 401


class TestPlaylistCrud:
    def test_list_reports_playlists_findings_and_the_active_one(self, client, protocol):
        protocol.set_playlist_name("bando_only")
        body = client.get("/api/playlists").json()
        assert set(body["playlists"]) == {"all_official_races", "bando_only"}
        assert body["active"] == "bando_only"
        assert body["findings"]["bando_only"] == []
        assert body["master_tracks_available"] is True
        assert "all_official_races" in body["protected"]

    def test_create_saves_and_republishes_the_name_list(self, client, protocol, project):
        res = client.put("/api/playlists/green_only", json={"items": [entry("The Green")]})
        assert res.status_code == 200
        saved = json.load(open(project["playlists_path"]))
        assert "green_only" in saved
        # available_playlists.txt is what the in-game /playlist command lists; it must not
        # drift from playlists.json.
        assert set(protocol.read_lines("available_playlists.txt")) == set(saved)

    def test_blocking_validation_is_a_400_with_findings(self, client, project):
        res = client.put("/api/playlists/typo", json={"items": [entry(env="Bandoo City")]})
        assert res.status_code == 400
        detail = res.json()["detail"]
        assert detail["findings"][0]["code"] == "UNKNOWN_ENVIRONMENT"
        assert "typo" not in json.load(open(project["playlists_path"]))

    def test_warnings_need_force(self, client, project):
        payload = {"items": [entry(track="no such track")]}
        assert client.put("/api/playlists/maybe", json=payload).status_code == 400
        payload["force"] = True
        assert client.put("/api/playlists/maybe", json=payload).status_code == 200
        assert "maybe" in json.load(open(project["playlists_path"]))

    def test_validate_endpoint_never_saves(self, client, project):
        res = client.post("/api/playlists/dry_run/validate",
                          json={"items": [entry(track="nope")]})
        assert res.status_code == 200
        assert res.json()["warnings"] > 0
        assert res.json()["blocking"] == 0
        assert "dry_run" not in json.load(open(project["playlists_path"]))

    def test_delete_removes_it_and_republishes(self, client, protocol, project):
        assert client.delete("/api/playlists/bando_only").status_code == 200
        assert "bando_only" not in json.load(open(project["playlists_path"]))
        assert "bando_only" not in protocol.read_lines("available_playlists.txt")

    def test_delete_refuses_the_fallback_and_the_active_playlist(self, client, protocol):
        assert client.delete("/api/playlists/all_official_races").status_code == 400
        protocol.set_playlist_name("bando_only")
        res = client.delete("/api/playlists/bando_only")
        assert res.status_code == 400
        assert "active playlist" in res.json()["detail"]["message"]


class TestActivate:
    def test_writes_the_name_and_resolves_the_rotation(self, client, protocol):
        res = client.post("/api/playlists/bando_only/activate")
        assert res.status_code == 200
        assert res.json()["resolved_tracks"] == 2
        assert protocol.read_text("playlist_name.txt") == "bando_only"
        tracks = protocol.read_rotation_tracks()
        assert [t["track"] for t in tracks] == ["BC Track 0", "BC Track 1"]
        assert all(t["mode"] == "Classic Race" for t in tracks)

    def test_activation_resets_the_plugin_owned_cursor(self, client, protocol):
        protocol.reset_rotation_state()
        with open(protocol.path("rotation_state.txt"), "w") as f:
            f.write("7")  # simulate the plugin having advanced the cursor mid-playlist
        client.post("/api/playlists/bando_only/activate")
        assert protocol.read_text("rotation_state.txt") == "0"

    def test_signal_only_activation_skips_resolution(self, client, protocol):
        res = client.post("/api/playlists/bando_only/activate", json={"resolve": False})
        assert res.json()["resolved_tracks"] is None
        assert protocol.read_text("playlist_name.txt") == "bando_only"
        # The orchestrator's playlist_name.txt watcher does the resolution in this mode.
        assert protocol.read_rotation_tracks() == []

    def test_unknown_playlist_is_404(self, client):
        assert client.post("/api/playlists/nope/activate").status_code == 404

    def test_missing_master_list_is_a_409_explaining_why(self, client, project, protocol):
        import os

        os.remove(project["master_path"])
        res = client.post("/api/playlists/bando_only/activate")
        assert res.status_code == 409
        assert "gather_tracks.py" in res.json()["detail"]
        # The name was still written, so the orchestrator can retry the resolution.
        assert protocol.read_text("playlist_name.txt") == "bando_only"


class TestControls:
    def test_interval(self, client, protocol):
        assert client.post("/api/control/interval", json={"seconds": 240}).status_code == 200
        assert protocol.read_text("rotation_interval.txt") == "240"

    def test_interval_is_bounded(self, client):
        assert client.post("/api/control/interval", json={"seconds": 0}).status_code == 422
        assert client.post("/api/control/interval", json={"seconds": 999999}).status_code == 422

    def test_lobby_name_visibility_and_max_players(self, client, protocol):
        res = client.post("/api/control/lobby",
                          json={"name": "  Bar's Bot  ", "private": False, "max_players": 8})
        assert res.status_code == 200
        assert protocol.read_text("lobby_name.txt") == "Bar's Bot"
        assert protocol.read_text("room_private.txt") == "false"
        assert protocol.read_text("max_players.txt") == "8"

    def test_lobby_accepts_a_partial_update(self, client, protocol):
        protocol.set_room_private(True)
        client.post("/api/control/lobby", json={"name": "Only The Name"})
        assert protocol.read_text("lobby_name.txt") == "Only The Name"
        assert protocol.read_text("room_private.txt") == "true"

    def test_lobby_rejects_empty_name_and_empty_body(self, client):
        assert client.post("/api/control/lobby", json={"name": "   "}).status_code == 400
        assert client.post("/api/control/lobby", json={}).status_code == 400

    def test_auto_start_and_democracy_flags(self, client, protocol):
        client.post("/api/control/auto-start", json={"enabled": True})
        client.post("/api/control/democracy", json={"enabled": True})
        assert protocol.read_flag("auto_start.txt") is True
        assert protocol.read_flag("democracy_mode.txt") is True
        client.post("/api/control/auto-start", json={"enabled": False})
        assert protocol.read_flag("auto_start.txt") is False

    def test_shuffle_toggle_invalidates_the_plugin_owned_deal(self, client, protocol):
        with open(protocol.path("shuffle_order.txt"), "w") as f:
            f.write("# signature:deadbeef\n2\n0\n1\n")
        assert client.post("/api/control/shuffle", json={"enabled": True}).status_code == 200
        assert protocol.read_flag("shuffle_mode.txt") is True
        assert not protocol.exists("shuffle_order.txt")

    def test_rotation_pause_and_engage(self, client, protocol):
        client.post("/api/control/rotation", json={"paused": True})
        assert protocol.read_flag("rotation_paused.txt") is True
        client.post("/api/control/rotation", json={"paused": False, "engaged": False})
        assert protocol.read_flag("rotation_paused.txt") is False
        assert protocol.read_flag("rotation_engaged.txt", True) is False
        assert client.post("/api/control/rotation", json={}).status_code == 400

    def test_game_mode_override_set_and_clear(self, client, protocol):
        client.post("/api/control/game-mode", json={"mode": "Dropout Race"})
        assert protocol.read_text("override_game_mode.txt") == "Dropout Race"
        client.post("/api/control/game-mode", json={"mode": None})
        assert not protocol.exists("override_game_mode.txt")

    def test_maintenance_is_presence_based(self, client, protocol):
        client.post("/api/control/maintenance", json={"enabled": True})
        assert protocol.exists("maintenance_active.txt")
        # Cancel must DELETE: the plugin only checks presence, so "false" would still
        # schedule a shutdown.
        client.post("/api/control/maintenance", json={"enabled": False})
        assert not protocol.exists("maintenance_active.txt")


class TestSkip:
    # bot-dashboard.md SPEC CONFLICT resolution (staging): HandleGameRoom now polls for
    # skip_now.txt and consumes it into the same skipRequested flag /skip sets, so the
    # dashboard writes a real one-shot file instead of returning 501.
    def test_skip_writes_the_one_shot_file(self, client, protocol):
        res = client.post("/api/control/skip")
        assert res.status_code == 200
        assert res.json() == {"skip_requested": True}
        assert protocol.exists("skip_now.txt")

    def test_control_info_advertises_support(self, client):
        body = client.get("/api/control/info").json()
        assert body["skip_supported"] is True
        assert body["skip_reason"] is None
        assert "skip_now.txt" in body["writable_files"]
        assert "rotation_state.txt" in body["plugin_owned_files"]


class TestRestartEndpoint:
    def test_disabled_by_default_returns_503(self, client):
        res = client.post("/api/control/restart")
        assert res.status_code == 503
        assert "disabled" in res.json()["detail"]

    def test_enabled_runs_the_configured_command_for_real(self, project, settings, token):
        """Runs an actual (harmless) command rather than mocking subprocess: the point of
        this endpoint is that it executes something, and a mock that silently fails open
        would have run `docker restart` on the developer's machine instead."""
        from fastapi.testclient import TestClient

        from dashboard.api import create_app

        settings.allow_container_restart = True
        settings.restart_command = ["echo", "fpv-bot-restarted"]
        app = create_app(settings, plugins_dir=project["plugins_dir"], log_dir=project["log_dir"])
        with TestClient(app) as client:
            client.headers.update({"X-Auth-Token": token})
            res = client.post("/api/control/restart")
        assert res.status_code == 200
        assert res.json()["stdout"] == "fpv-bot-restarted"

    def test_a_failing_command_is_a_500_with_its_output(self, project, settings, token):
        from fastapi.testclient import TestClient

        from dashboard.api import create_app

        settings.allow_container_restart = True
        settings.restart_command = ["false"]
        app = create_app(settings, plugins_dir=project["plugins_dir"], log_dir=project["log_dir"])
        with TestClient(app) as client:
            client.headers.update({"X-Auth-Token": token})
            res = client.post("/api/control/restart")
        assert res.status_code == 500
        assert "exited 1" in res.json()["detail"]


class TestOwnershipIsEnforcedThroughTheApi:
    def test_no_route_can_content_write_plugin_owned_state(self, client, protocol):
        """The API surface exposes no way to write rotation_state.txt / shuffle_order.txt
        with content -- only the sanctioned resets. This is the AGENTS.md rule 4 guard at
        the HTTP boundary rather than only in the service layer."""
        protocol.reset_rotation_state()
        before = protocol.read_text("rotation_state.txt")
        # There is deliberately no /api/control/rotation-state endpoint.
        assert client.post("/api/control/rotation-state", json={"index": 5}).status_code == 404
        assert protocol.read_text("rotation_state.txt") == before
