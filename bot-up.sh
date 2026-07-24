#!/usr/bin/env bash
# bot-up.sh — start the Dockerized Liftoff bot and wait for it to come up.
#
# Supersedes restart_bot.sh (bare-metal era) for normal operation. Operator
# directive 2026-07-17: the bot runs in Docker only.
#
# NEVER run `docker compose down -v` (or otherwise remove volumes). The
# external `agent-af473f774bb75bf19_*` volumes hold a primed ~22.5G Steam
# install + a live Steam login — deleting them forces a full re-download and
# re-login. This script only brings the container up; it never touches
# volumes, the Steam client, steamcmd, or the container entrypoint.
set -euo pipefail

CONTAINER="procedural-fpv-bot-1"
PLAYER_LOG="/steam/.config/unity3d/LuGus Studios/Liftoff/Player.log"
TIMEOUT_S=$((10 * 60))
AUTO_RECOVER=0

for arg in "$@"; do
  case "$arg" in
    --auto-recover) AUTO_RECOVER=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! docker ps >/dev/null 2>&1; then
  echo "ERROR: cannot talk to the Docker daemon as $(whoami)." >&2
  echo "Check that Docker is running and this user is in the 'docker' group (no sudo should be needed)." >&2
  exit 1
fi

echo "Starting bot container via docker compose up -d ..."
docker compose up -d

recover_once_done=0

# Player.log is volume-persisted and survives across container restarts —
# `compose up -d` does NOT truncate it. If we grepped the whole file right
# away, a leftover "SteamAPI.Init() returned True" / "room_entered" (or the
# flake line) from the PREVIOUS session would read as fresh evidence and
# give a false SUCCESS (same trap as the known readiness-gate stale-'OK'
# race). The game only truncates Player.log when it actually relaunches, so:
# capture the log's current byte size, and refuse to evaluate any signal
# until the size has shrunk (truncation = fresh boot began) or there was no
# log yet (size 0 = anything written is fresh). Re-arm this guard after a
# recovery pkill for the same reason — that relaunch truncates it again.
initial_size="$(docker exec "$CONTAINER" sh -c "wc -c < '$PLAYER_LOG'" 2>/dev/null || echo 0)"
fresh_boot=0

wait_for_signal() {
  local elapsed=0
  while (( elapsed < TIMEOUT_S )); do
    if (( fresh_boot == 0 )); then
      local current_size
      current_size="$(docker exec "$CONTAINER" sh -c "wc -c < '$PLAYER_LOG'" 2>/dev/null || echo 0)"
      if (( current_size < initial_size || initial_size == 0 )); then
        fresh_boot=1
      else
        sleep 5
        elapsed=$((elapsed + 5))
        continue
      fi
    fi

    local log_contents
    log_contents="$(docker exec "$CONTAINER" cat "$PLAYER_LOG" 2>/dev/null || true)"

    if grep -q "SteamAPI.Init() returned True" <<<"$log_contents" \
       && grep -q "room_entered" <<<"$log_contents"; then
      echo "SUCCESS: Steam initialized and room entered."
      return 0
    fi

    if grep -q "SteamAPI.Init() returned False." <<<"$log_contents"; then
      echo "Known flake detected: SteamAPI.Init() returned False."
      echo "Recovery: kill only the Liftoff game process inside the container;"
      echo "the orchestrator watchdog relaunches it automatically:"
      echo "  docker exec ${CONTAINER} pkill -f Liftoff.x86_64"
      echo "Never restart the Steam client, re-run the entrypoint, or touch steamcmd."

      if (( AUTO_RECOVER == 1 && recover_once_done == 0 )); then
        echo "--auto-recover: running the pkill once, then continuing to wait ..."
        docker exec "$CONTAINER" pkill -f Liftoff.x86_64 || true
        recover_once_done=1
        # Player.log resets on relaunch — re-arm the stale-log guard so we
        # wait for the fresh truncation instead of re-grepping this same
        # (now stale) content again.
        initial_size="$(docker exec "$CONTAINER" sh -c "wc -c < '$PLAYER_LOG'" 2>/dev/null || echo 0)"
        fresh_boot=0
      else
        return 1
      fi
    fi

    sleep 5
    elapsed=$((elapsed + 5))
  done

  echo "TIMEOUT: no success or recognized flake signal within ${TIMEOUT_S}s." >&2
  return 1
}

wait_for_signal
