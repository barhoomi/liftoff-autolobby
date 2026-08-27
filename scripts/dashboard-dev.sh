#!/bin/bash
# Local dashboard dev server with hot reload -- edit dashboard/**, browser refresh, done.
# Never touches the live box; everything resolves inside this checkout (config/ + logs/).
#
#   bash scripts/dashboard-dev.sh              # http://127.0.0.1:8770, token "dev"
#   FPV_DASHBOARD_TOKEN=s3cret bash scripts/dashboard-dev.sh --port 9000
#
# Anything after the script name is passed straight to `python -m dashboard`.
#
# The event feed shows whatever JSONL files sit in ./logs (gitignored). For realistic
# data, copy a day off the live box first -- see dashboard/README.md "Real data locally".
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x venv/bin/python3 ]; then
    echo "--- creating venv + installing requirements-dev.txt (first run only) ---"
    python3 -m venv venv
    venv/bin/pip install -q --upgrade pip
    venv/bin/pip install -q -r requirements-dev.txt
fi

mkdir -p logs

# "dev" is fine here: the server binds 127.0.0.1 (DEFAULT_HOST) unless you pass --host.
export FPV_DASHBOARD_TOKEN="${FPV_DASHBOARD_TOKEN:-dev}"
export FPV_LOG_DIR="${FPV_LOG_DIR:-$PWD/logs}"

echo "--- token: $FPV_DASHBOARD_TOKEN   logs: $FPV_LOG_DIR ---"
# See scripts/run_tests.sh for why PYTHONPATH must be cleared around the venv.
exec env -u PYTHONPATH venv/bin/python3 -m dashboard --reload "$@"
