"""Tests for the dashboard's read-side HTTP API (state, events, SSE, log browser).

Everything runs against the throwaway project tree from conftest.py — no game, no bot,
no real config touched.
"""

import json


class TestAuth:
    def test_health_needs_no_token(self, anon_client):
        assert anon_client.get("/api/health").status_code == 200

    def test_protected_endpoints_reject_a_missing_token(self, anon_client):
        for path in ("/api/state", "/api/events/recent", "/api/logs", "/api/auth/check"):
            assert anon_client.get(path).status_code == 401, path

    def test_wrong_token_is_rejected(self, anon_client):
        assert anon_client.get("/api/state", headers={"X-Auth-Token": "nope"}).status_code == 401

    def test_bearer_header_is_accepted(self, anon_client, token):
        res = anon_client.get("/api/auth/check", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200

    def test_query_parameter_is_accepted_for_eventsource(self, anon_client, token):
        # EventSource cannot set headers, so the SSE endpoint has to accept ?token=.
        assert anon_client.get(f"/api/auth/check?token={token}").status_code == 200

    def test_an_app_without_a_token_refuses_everything(self, project):
        from fastapi.testclient import TestClient

        from dashboard.api import create_app
        from dashboard.control.settings import DashboardSettings

        app = create_app(DashboardSettings(token=None, project_dir=project["root"]),
                         plugins_dir=project["plugins_dir"], log_dir=project["log_dir"])
        with TestClient(app) as client:
            assert client.get("/api/state").status_code == 503


class TestState:
    def test_empty_install_returns_a_well_formed_snapshot(self, client):
        body = client.get("/api/state").json()
        assert body["config"]["track_count"] == 0
        assert body["rotation"]["current_track"] is None
        assert body["live"]["players"] == []

    def test_reflects_protocol_files_and_events(self, client, protocol, write_event):
        protocol.set_lobby_name("Bar's Bot")
        protocol.set_rotation_interval(90)
        protocol.set_playlist_name("bando_only")
        protocol.write_text("tracks_to_rotate.txt", "# hdr\nBC Track 0,Bando City,Race\n")
        write_event("rotation", track="BC Track 0", env="Bando City", mode="Race", index=0)
        write_event("player_join", player="alice", userId="steam_1", count=2)

        body = client.get("/api/state").json()
        assert body["config"]["lobby_name"] == "Bar's Bot"
        assert body["config"]["playlist"] == "bando_only"
        assert body["rotation"]["current_track"] == "BC Track 0"
        assert body["rotation"]["interval_s"] == 90
        assert [p["player"] for p in body["live"]["players"]] == ["alice"]

    def test_missing_plugins_dir_reports_503_not_a_stack_trace(self, project, settings, token):
        from fastapi.testclient import TestClient

        from dashboard.api import create_app

        # No liftoff_path and no override -> the control plane cannot locate the
        # protocol directory. That is a configuration problem, and it should read like
        # one rather than a 500.
        (project["config_dir"] + "/lobby_config.json")
        with open(project["config_dir"] + "/lobby_config.json", "w") as f:
            json.dump({"log_dir": project["log_dir"]}, f)
        app = create_app(settings, log_dir=project["log_dir"])
        with TestClient(app) as client:
            client.headers.update({"X-Auth-Token": token})
            res = client.get("/api/state")
            assert res.status_code == 503
            assert "plugins directory" in res.json()["detail"]


class TestEvents:
    def test_recent_returns_events_oldest_first(self, client, write_event):
        write_event("chat", player="alice", msg="gg")
        write_event("rotation", track="A", env="Bando City")
        body = client.get("/api/events/recent").json()
        assert [e["event"] for e in body["events"]] == ["chat", "rotation"]

    def test_recent_respects_the_limit(self, client, write_event):
        for i in range(10):
            write_event("chat", msg=str(i))
        body = client.get("/api/events/recent?limit=3").json()
        assert len(body["events"]) == 3
        assert body["events"][-1]["msg"] == "9"

    def test_limit_is_bounded(self, client):
        assert client.get("/api/events/recent?limit=0").status_code == 422
        assert client.get("/api/events/recent?limit=999999").status_code == 422

    def test_stream_requires_a_token(self, anon_client):
        # The route itself is four lines of plumbing around sse_event_source(); the
        # streaming behaviour is covered directly in TestSseEventSource below, because an
        # endless response body deadlocks TestClient on teardown.
        with anon_client.stream("GET", "/api/events/stream") as res:
            assert res.status_code == 401


class TestSseEventSource:
    """Drives the SSE body coroutine directly (see its docstring for why)."""

    def _collect(self, log_dir, backlog=50, polls=1, poll_interval=0.0, **kwargs):
        import asyncio

        from dashboard.api import sse_event_source

        remaining = {"polls": polls}

        async def is_disconnected():
            if remaining["polls"] <= 0:
                return True
            remaining["polls"] -= 1
            return False

        async def run():
            chunks = []
            async for chunk in sse_event_source(log_dir, poll_interval, backlog,
                                                is_disconnected, **kwargs):
                chunks.append(chunk)
            return chunks

        return asyncio.run(run())

    def _events(self, chunks):
        return [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]

    def test_replays_the_backlog_first(self, project, write_event):
        write_event("chat", player="alice", msg="hello")
        write_event("rotation", track="A", env="Bando City")
        chunks = self._collect(project["log_dir"], polls=0)
        assert [e["event"] for e in self._events(chunks)] == ["chat", "rotation"]

    def test_backlog_zero_replays_nothing(self, project, write_event):
        write_event("chat", msg="hello")
        assert self._collect(project["log_dir"], backlog=0, polls=0) == []

    def test_streams_lines_appended_after_connect(self, project, write_event):
        write_event("chat", msg="old")
        # One poll happens before the new line exists, one after -- the second must pick
        # it up without re-emitting the backlog.
        import threading

        def append_later():
            write_event("rotation", track="Next", env="The Green")

        chunks = []
        chunks.extend(self._collect_with_writer(project["log_dir"], append_later))
        events = self._events(chunks)
        assert [e["event"] for e in events] == ["chat", "rotation"]

    def _collect_with_writer(self, log_dir, writer):
        import asyncio

        from dashboard.api import sse_event_source

        state = {"polls": 0}

        async def is_disconnected():
            state["polls"] += 1
            if state["polls"] == 1:
                writer()
                return False
            return state["polls"] > 2

        async def run():
            chunks = []
            async for chunk in sse_event_source(log_dir, 0.0, 50, is_disconnected):
                chunks.append(chunk)
            return chunks

        return asyncio.run(run())

    def test_emits_a_heartbeat_when_idle(self, project):
        ticks = iter([0.0, 100.0, 200.0, 300.0, 400.0])
        chunks = self._collect(project["log_dir"], polls=2,
                               heartbeat_seconds=1.0, clock=lambda: next(ticks))
        assert ": keep-alive\n\n" in chunks

    def test_no_heartbeat_while_events_are_flowing(self, project, write_event):
        write_event("chat", msg="x")
        chunks = self._collect(project["log_dir"], polls=1, heartbeat_seconds=1.0)
        assert all(not c.startswith(":") for c in chunks)

    def test_unresolved_log_dir_says_so_and_stops(self, project):
        chunks = self._collect(None, polls=5)
        assert len(chunks) == 1
        assert self._events(chunks)[0]["event"] == "_dashboard"


class TestLogs:
    def test_catalogue_lists_the_daily_jsonl_and_the_plugin_log(self, client, write_event):
        write_event("chat", msg="x")
        logs = client.get("/api/logs").json()["logs"]
        ids = [entry["id"] for entry in logs]
        assert any(i.startswith("jsonl:bot-") for i in ids)
        assert "bepinex" in ids

    def test_tail_returns_the_file_text(self, client, write_event):
        write_event("chat", msg="hello world")
        logs = client.get("/api/logs").json()["logs"]
        jsonl_id = next(e["id"] for e in logs if e["kind"] == "jsonl")
        body = client.get(f"/api/logs/{jsonl_id}/tail?lines=10").json()
        assert "hello world" in body["text"]

    def test_unknown_log_id_is_404(self, client):
        assert client.get("/api/logs/nope/tail").status_code == 404

    def test_path_traversal_is_not_reachable(self, client):
        for bad in ("..%2F..%2Fetc%2Fpasswd", "jsonl:..%2F..%2Fetc%2Fpasswd"):
            assert client.get(f"/api/logs/{bad}/tail").status_code == 404

    def test_missing_but_catalogued_file_is_404_with_its_path(self, client, project):
        import os

        os.remove(os.path.join(project["game_dir"], "BepInEx", "LogOutput.log"))
        res = client.get("/api/logs/bepinex/tail")
        assert res.status_code == 404
        assert "LogOutput.log" in res.json()["detail"]


class TestStaticUi:
    def test_index_is_served(self, anon_client):
        res = anon_client.get("/")
        assert res.status_code == 200
        assert "Liftoff bot" in res.text

    def test_assets_are_served(self, anon_client):
        assert anon_client.get("/static/app.js").status_code == 200
        assert anon_client.get("/static/styles.css").status_code == 200
