"""FastAPI app for the bot dashboard (decision D4: FastAPI + SSE + vanilla JS).

Read side: a live SSE tail of the shared JSONL event log, a derived current-state
snapshot, and a raw log browser. Write side: playlists and bot controls, every one of
which goes through ``dashboard.control`` rather than touching a file directly.

Auth is decision D2: one shared token, no users. It may arrive as ``Authorization:
Bearer <token>``, an ``X-Auth-Token`` header, or a ``token`` query parameter — the last
one exists because ``EventSource`` cannot set headers, which is the whole reason a
token-in-URL is tolerable here (D1: this is bound to localhost/LAN behind a tunnel, not
the internet). Comparison is constant-time.
"""

import asyncio
import hmac
import os
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .control import ProtocolDir, resolve_plugins_dir
from .control import events as events_mod
from .control import logs as logs_mod
from .control import paths as paths_mod
from .control import state as state_mod
from .control.settings import load_settings

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# SSE keep-alive. Without traffic, an idle proxy (or a phone that backgrounded the tab)
# can drop the connection silently; a comment line is the cheapest way to keep it warm
# and is ignored by EventSource.
HEARTBEAT_SECONDS = 15.0


class DashboardContext:
    """Everything a request handler needs, resolved fresh enough to survive an operator
    editing lobby_config.json without restarting the dashboard."""

    def __init__(self, settings, plugins_dir=None, log_dir=None):
        self.settings = settings
        self._plugins_dir_override = plugins_dir
        self._log_dir_override = log_dir

    @property
    def project_dir(self):
        return self.settings.project_dir

    def config(self):
        return paths_mod.load_lobby_config_or_empty(self.project_dir)

    def plugins_dir(self):
        if self._plugins_dir_override:
            return self._plugins_dir_override
        return resolve_plugins_dir(self.config(), self.project_dir)

    def protocol(self):
        plugins_dir = self.plugins_dir()
        if not plugins_dir:
            raise HTTPException(
                status_code=503,
                detail="Cannot locate the BepInEx plugins directory. Set liftoff_path in "
                       "config/lobby_config.json, or FPV_PLUGINS_DIR.")
        return ProtocolDir(plugins_dir)

    def log_dir(self):
        if self._log_dir_override:
            return self._log_dir_override
        return paths_mod.resolve_log_dir(self.config(), self.project_dir)


def _extract_token(request):
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("x-auth-token")
    if header:
        return header.strip()
    return request.query_params.get("token")


def create_app(settings=None, plugins_dir=None, log_dir=None):
    settings = settings or load_settings()
    ctx = DashboardContext(settings, plugins_dir=plugins_dir, log_dir=log_dir)

    app = FastAPI(title="Liftoff bot dashboard", docs_url=None, redoc_url=None)
    app.state.ctx = ctx

    def require_token(request: Request):
        supplied = _extract_token(request)
        if not settings.token:
            # Should be unreachable (__main__ refuses to start tokenless), but a
            # programmatically constructed app must not silently become open.
            raise HTTPException(status_code=503, detail="Dashboard token is not configured.")
        if not supplied or not hmac.compare_digest(supplied, settings.token):
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        return True

    auth = [Depends(require_token)]

    # --- unauthenticated: liveness only, leaks nothing ------------------------

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "liftoff-bot-dashboard"}

    # --- read side ------------------------------------------------------------

    @app.get("/api/auth/check", dependencies=auth)
    def auth_check():
        return {"ok": True}

    @app.get("/api/state", dependencies=auth)
    def get_state(limit: int = Query(500, ge=1, le=5000)):
        return state_mod.build_snapshot(ctx.protocol(), ctx.log_dir(), limit=limit)

    @app.get("/api/events/recent", dependencies=auth)
    def recent_events(limit: int = Query(200, ge=1, le=5000),
                      days: int = Query(2, ge=1, le=30)):
        log_dir = ctx.log_dir()
        return {"log_dir": log_dir,
                "events": events_mod.read_recent(log_dir, limit=limit, days=days)}

    @app.get("/api/events/stream", dependencies=auth)
    async def stream_events(request: Request,
                            backlog: int = Query(50, ge=0, le=1000)):
        generate = sse_event_source(ctx.log_dir(), settings.poll_interval, backlog,
                                    request.is_disconnected)
        return StreamingResponse(generate, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Neutralize proxy buffering, which otherwise holds SSE frames until a
            # buffer fills and makes a "live" stream arrive in bursts minutes later.
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/logs", dependencies=auth)
    def list_logs():
        return {"logs": logs_mod.list_logs(ctx.config(), ctx.project_dir, ctx.log_dir())}

    @app.get("/api/logs/{log_id}/tail", dependencies=auth)
    def tail_log(log_id: str, lines: int = Query(200, ge=1, le=5000)):
        entry = logs_mod.find_log(log_id, ctx.config(), ctx.project_dir, ctx.log_dir())
        if entry is None:
            raise HTTPException(status_code=404, detail=f"No such log: {log_id}")
        if not entry["exists"]:
            raise HTTPException(status_code=404, detail=f"{entry['path']} does not exist")
        try:
            text = logs_mod.tail_file(entry["path"], lines=lines)
        except logs_mod.LogUnreadableError as e:
            raise HTTPException(status_code=403, detail=f"Cannot read {entry['path']}: {e}")
        return {"id": entry["id"], "path": entry["path"], "lines": lines, "text": text}

    # --- static UI ------------------------------------------------------------

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    return app


def _sse(payload):
    import json
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def sse_event_source(log_dir, poll_interval, backlog, is_disconnected,
                           heartbeat_seconds=HEARTBEAT_SECONDS, clock=time.monotonic):
    """The SSE body: backlog replay, then one frame per new JSONL line, forever.

    Deliberately a free coroutine rather than a closure inside the route, so it can be
    driven directly in tests: an endless generator behind ``TestClient`` deadlocks on
    teardown (the client waits for a body that never ends), and the interesting behaviour
    — backlog replay, tailing, heartbeats, stopping on disconnect — is all in here rather
    than in the four lines of HTTP plumbing that wrap it.

    ``is_disconnected`` is an awaitable predicate (Starlette's ``Request.is_disconnected``
    in production); returning True ends the stream.
    """
    if not log_dir:
        yield _sse({"event": "_dashboard", "message": "No log directory resolved."})
        return

    for event in events_mod.read_recent(log_dir, limit=backlog):
        yield _sse(event)

    tail = events_mod.EventTail(log_dir).seek_to_end()
    last_beat = clock()
    while True:
        if await is_disconnected():
            return
        new_events = await asyncio.to_thread(tail.poll)
        for event in new_events:
            yield _sse(event)
        now = clock()
        if new_events:
            last_beat = now
        elif now - last_beat >= heartbeat_seconds:
            last_beat = now
            # A comment frame: ignored by EventSource, but it keeps an idle proxy (or a
            # backgrounded phone tab) from silently dropping the connection.
            yield ": keep-alive\n\n"
        await asyncio.sleep(poll_interval)


__all__ = ["create_app", "DashboardContext"]
