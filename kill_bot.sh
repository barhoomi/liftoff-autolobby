#!/bin/bash
# kill_bot.sh - Stop all bot, Liftoff, and Steam processes run by fpv_bot
#
# Usage:
#   bash kill_bot.sh
#

set -e

echo "=== Stopping Liftoff FPV Bot & Steam processes run by fpv_bot ==="

# Target processes to locate and stop: pattern:pgrep_flag
# We use -f (full command line) for scripts, and -x (exact executable name) for binary executables
TARGETS=(
    "run_headless_lobby.py:-f"
    "Liftoff.x86_64:-x"
    "run_bepinex.sh:-f"
    "steam:-x"
    "steamwebhelper:-x"
    "Xvfb:-x"
)

# Helper to find PIDs owned by fpv_bot
get_pids() {
    local target="$1"
    local pattern="${target%%:*}"
    local flag="${target##*:}"
    sudo -u fpv_bot -n pgrep -u fpv_bot "$flag" "$pattern" 2>/dev/null || true
}

# 1. First attempt: Graceful termination (SIGTERM)
echo "Sending SIGTERM to target processes..."
for target in "${TARGETS[@]}"; do
    pattern="${target%%:*}"
    pids=$(get_pids "$target")
    if [[ -n "$pids" ]]; then
        pid_list=$(echo "$pids" | tr '\n' ' ')
        echo "  - Found '$pattern' processes (PIDs: $pid_list). Sending SIGTERM..."
        sudo -u fpv_bot -n kill -15 $pids 2>/dev/null || true
    fi
done

# Wait for processes to exit gracefully
echo "Waiting 3 seconds for graceful shutdown..."
sleep 3

# 2. Second attempt: Forceful termination (SIGKILL)
echo "Checking for remaining processes..."
any_remaining=0
for target in "${TARGETS[@]}"; do
    pattern="${target%%:*}"
    pids=$(get_pids "$target")
    if [[ -n "$pids" ]]; then
        pid_list=$(echo "$pids" | tr '\n' ' ')
        echo "  - WARNING: '$pattern' processes still running (PIDs: $pid_list). Sending SIGKILL..."
        sudo -u fpv_bot -n kill -9 $pids 2>/dev/null || true
        any_remaining=1
    fi
done

if [[ $any_remaining -eq 0 ]]; then
    echo "All target processes terminated successfully."
else
    echo "Remaining target processes forcefully terminated."
fi

echo "=== Bot shutdown complete ==="
