# Dashboard control plane

What's left of `dashboard/` in this repo is the **control plane only** —
`dashboard/control/` plus this package's `__init__.py` and the control-plane tests.
It owns playlist resolution and every write to the plain-text plugin protocol files;
the orchestrator imports it at runtime (`from dashboard.control import ...`), which is
why it lives here with the bot and stays pure-stdlib-plus-nothing (no FastAPI).

The **web app layer** — `api.py`, `write_api.py`, `__main__.py`, `static/`, the API
tests, and the whole edit→live workflow — moved to the private
[barhoomi/liftoff-dashboard](https://github.com/barhoomi/liftoff-dashboard) repo.
Its README carries the dev-server, deploy, and CI/CD documentation.

## How the two repos meet in production

The live sidecar still runs `python3 -m dashboard` from the image, with the
`docker-compose.dashboard-dev.yml` overlay bind-mounting
`~/liftoff-autolobby/dashboard` (this repo's clone on the box) over `/app/dashboard`:

- `control/` in that directory is **tracked by this repo** and updates via `git pull`.
- The app files land there **untracked**, rsync'd by the private repo's deploys
  (`scripts/deploy.sh` there, or its Deploy workflow on push to main). They are
  excluded from that rsync's `--delete`, and `git pull` here ignores them.

The image still bakes the app's runtime deps (`requirements-dashboard.txt`:
fastapi/uvicorn/pydantic) so the bind-mounted app can import them — that file stays in
this repo because the Dockerfile is here.

**After checking out a ref where the app files were removed** (this one), the deployed
directory keeps working only once a private-repo deploy has re-landed the app files as
untracked content — run one deploy after pulling this on the box.

## Tests

`dashboard/tests/` here covers only the control plane and runs with the repo's plain
`requirements-dev.txt` (no web deps). The API tests live in the private repo next to
the code they test.
