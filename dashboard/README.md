# Dashboard sidecar

FastAPI monitoring/control panel for the bot. Runs as its own container (`dashboard`
service in `docker-compose.yml`) from the **same image** as the bot, with the entrypoint
overridden to `python3 -m dashboard` — it shares the bot's volumes (event log read-only,
config read-write, plugin protocol files) but never runs any Steam/game code. The bot
does not know the dashboard exists; killing, breaking or redeploying the dashboard
cannot touch the running lobby.

Coupling to the rest of the repo, in full:

- `dashboard/control/eventlog.py` re-exports `orchestrator/event_log.py`'s log-dir
  resolver (defensively — absence means "structured logging off").
- `dashboard/control/playlists.py` owns `resolve_and_write_playlist()` (the D5
  extraction out of the orchestrator) and imports `trackcheck.playlist_match` /
  `trackcheck.lint_playlists`.
- Everything else under `dashboard/` is self-contained. The web deps (fastapi, uvicorn,
  pydantic — `requirements-dashboard.txt`) are the only third-party runtime packages in
  the whole image; `dashboard/control/` deliberately stays importable without them.

Auth is a single shared token (`FPV_DASHBOARD_TOKEN` env, or
`lobby_config.json → {"dashboard": {"token": ...}}`). No default — the app refuses to
start without one.

## Edit → see it locally (seconds)

```bash
bash scripts/dashboard-dev.sh        # http://127.0.0.1:8770, token "dev"
```

Creates the venv on first run, then serves with uvicorn `--reload`: edit any file under
`dashboard/`, the worker restarts in ~1s; static files (`static/*.html/js/css`) are read
from disk per-request and need no restart at all. Tests: `bash scripts/run_tests.sh`
(whole suite) or `venv/bin/python3 -m pytest dashboard/tests -q`.

### Real data locally

The dev server's event feed reads `./logs/*.jsonl` (gitignored, empty by default).
Copy a day or two from the live box for realistic screens:

```bash
mkdir -p logs && scp liftoff-box:liftoff-autolobby/logs/bot-*.jsonl logs/
```

(That path exists once the one-time setup below has been done; adjust to wherever you
keep a copy of the JSONL logs otherwise.)

## Edit → see it live (one rsync, ~2s)

Routine deploy, after the one-time setup below:

```bash
bash scripts/deploy-dashboard.sh
```

Runs `dashboard/tests`, rsyncs `dashboard/` to the box, and polls `/api/health` until
the reloaded worker answers. No docker commands, no image rebuild, and the bot container
is never touched. `DEPLOY_SKIP_TESTS=1` to skip the test gate.

### One-time server setup

The stack must run with the live-edit overlay so the container sees a host directory
instead of the code baked into the image. On the box (needs docker rights there):

```bash
# 1. A host-side home for the deployed code, owned by uid 1000 (the container's botuser):
mkdir -p ~/liftoff-autolobby && cd <the compose project dir>

# 2. Get docker-compose.dashboard-dev.yml next to docker-compose.yml (git pull / scp),
#    then recreate ONLY the dashboard service with the overlay:
DASHBOARD_SRC=$HOME/liftoff-autolobby/dashboard \
  docker compose -f docker-compose.yml -f docker-compose.dashboard-dev.yml up -d dashboard
```

Run `bash scripts/deploy-dashboard.sh` once *before* step 2 so the bind-mount source is
populated. If you don't know where the compose project dir lives:
`docker inspect <dashboard container> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`.

To drop back to image-only code (e.g. before an upgrade of the base image):
`docker compose up -d dashboard` from the same dir, without the `-f` overlay pair.

## When a full image rebuild IS needed

Only for changes **outside** `dashboard/`, or to `requirements-dashboard.txt`:
`docker compose build && docker compose up -d` on the box (mind the volume warnings at
the bottom of `docker-compose.yml` — never `down -v`). Dashboard-only work never needs
this path.
