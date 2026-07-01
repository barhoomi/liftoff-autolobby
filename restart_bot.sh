#!/bin/bash
# restart_bot.sh - Safely stop previous instances, rebuild, and start the FPV bot lobby.
#
# Usage:
#   bash restart_bot.sh [options]
#
# Options are forwarded directly to run_bot.sh (e.g. --playlist, --interval, --gui, --headless, --no-build)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== [1/2] Stopping existing bot processes ==="
bash "$SCRIPT_DIR/kill_bot.sh"

echo ""
echo "=== [2/2] Launching the bot ==="
bash "$SCRIPT_DIR/run_bot.sh" "$@"
