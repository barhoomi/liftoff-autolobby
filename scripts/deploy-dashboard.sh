#!/bin/bash
# Push local dashboard/ edits to the live sidecar and verify it came back up.
#
#   bash scripts/deploy-dashboard.sh                 # deploy to liftoff-box
#   DEPLOY_HOST=other-box bash scripts/deploy-dashboard.sh
#
# Requires the ONE-TIME server setup from dashboard/README.md: the stack running with
# docker-compose.dashboard-dev.yml, whose bind mount points at $DEPLOY_DIR/dashboard on
# the host. After that, a deploy is just this rsync -- uvicorn's --reload notices the
# changed files and restarts the worker in ~1s. No docker access, no image rebuild, no
# bot interruption.
#
# The dashboard's own tests run first, from this checkout's venv (create it with
# scripts/dashboard-dev.sh or run_tests.sh once). Deploying code that can't even pass
# its unit tests just moves the failure somewhere harder to read. DEPLOY_SKIP_TESTS=1
# skips them when you're iterating on something cosmetic.
set -euo pipefail
cd "$(dirname "$0")/.."

DEPLOY_HOST="${DEPLOY_HOST:-liftoff-box}"
DEPLOY_DIR="${DEPLOY_DIR:-liftoff-autolobby}"   # relative to $HOME on the box
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8800/api/health}"

if [ "${DEPLOY_SKIP_TESTS:-0}" != "1" ]; then
    echo "--- pytest dashboard/tests ---"
    env -u PYTHONPATH venv/bin/python3 -m pytest dashboard/tests -q
fi

echo "--- rsync dashboard/ -> $DEPLOY_HOST:$DEPLOY_DIR/dashboard ---"
rsync -az --delete \
    --exclude='__pycache__' --exclude='*.pyc' \
    dashboard/ "$DEPLOY_HOST:$DEPLOY_DIR/dashboard/"

# --reload debounces for a moment before restarting the worker; poll rather than guess.
echo "--- waiting for $HEALTH_URL on $DEPLOY_HOST ---"
for i in $(seq 1 15); do
    sleep 1
    if ssh "$DEPLOY_HOST" "curl -fsS --max-time 3 $HEALTH_URL" >/dev/null 2>&1; then
        echo "--- deployed: dashboard healthy after ${i}s ---"
        exit 0
    fi
done

echo "!!! dashboard did not answer $HEALTH_URL within 15s" >&2
echo "    inspect on the box:  ssh $DEPLOY_HOST, then docker compose logs dashboard" >&2
exit 1
