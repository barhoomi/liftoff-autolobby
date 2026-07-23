#!/usr/bin/env bash
# bot-down.sh — gracefully stop the Dockerized Liftoff bot.
#
# Supersedes kill_bot.sh (bare-metal era) for normal operation. Operator
# directive 2026-07-17: the bot runs in Docker only.
#
# NEVER run `docker compose down -v` (or otherwise remove volumes) from this
# script or by hand. The external `agent-af473f774bb75bf19_*` volumes hold a
# primed ~22.5G Steam install + a live Steam login. Deleting them means
# re-downloading Steam/the game and re-doing interactive login. This script
# only stops the container; it never touches volumes.
set -euo pipefail

CONTAINER="procedural-fpv-bot-1"

if ! docker ps >/dev/null 2>&1; then
  echo "ERROR: cannot talk to the Docker daemon as $(whoami)." >&2
  echo "Check that Docker is running and this user is in the 'docker' group (no sudo should be needed)." >&2
  exit 1
fi

echo "Stopping container: ${CONTAINER} ..."
docker stop "${CONTAINER}"

echo "Waiting for container to report Exited..."
for _ in $(seq 1 30); do
  status="$(docker ps -a --filter "name=^${CONTAINER}\$" --format '{{.Status}}' || true)"
  if [[ "${status}" == Exited* ]]; then
    echo "Confirmed: ${CONTAINER} -> ${status}"
    exit 0
  fi
  sleep 1
done

echo "WARNING: container did not report Exited within timeout. Current status:" >&2
docker ps -a --filter "name=^${CONTAINER}\$" --format '{{.Names}}: {{.Status}}' >&2
exit 1
