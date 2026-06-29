#!/bin/bash
# run_bot.sh — build, deploy, and start the Liftoff bot in one command.
#
# Usage:
#   bash run_bot.sh [options]
#
# Options:
#   --playlist NAME      Playlist from playlists.json (default: all_official_races)
#   --interval SECONDS   Track rotation interval (default: 90)
#   --shuffle            Randomize playlist order
#   --public             Make the lobby public
#   --headless           Use Xvfb virtual display instead of GUI
#   --no-build           Skip build/sync step (faster restart)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Defaults ---
PLAYLIST="all_official_races"
INTERVAL=90
SHUFFLE=""
PUBLIC=""
HEADLESS=0
BUILD=1

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --playlist)  PLAYLIST="$2"; shift 2 ;;
        --interval)  INTERVAL="$2"; shift 2 ;;
        --shuffle)   SHUFFLE="--shuffle"; shift ;;
        --public)    PUBLIC="--public"; shift ;;
        --headless)  HEADLESS=1; shift ;;
        --no-build)  BUILD=0; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# --- Step 1: Build plugin and sync to fpv_bot ---
if [[ $BUILD -eq 1 ]]; then
    echo "=== [1/3] Building plugin and syncing to fpv_bot ==="
    bash "$SCRIPT_DIR/build.sh"
else
    echo "=== [1/3] Skipping build (--no-build) ==="
fi

# --- Step 2: Grant display access and start Steam ---
echo ""
echo "=== [2/3] Setting up display and Steam ==="
xhost +SI:localuser:fpv_bot

if pgrep -u fpv_bot steam > /dev/null 2>&1; then
    echo "Steam is already running as fpv_bot."
else
    echo "Starting Steam as fpv_bot..."
    sudo -u fpv_bot -H env XDG_RUNTIME_DIR= dbus-run-session /usr/games/steam &

    echo ""
    echo "  NOTE: If this is the first run, log into the 'Bar's Bot' Steam account"
    echo "  in the Steam window that just opened, then re-run with --no-build."
    echo ""
    echo "  Waiting 15s for Steam to initialize..."
    sleep 15
fi

# --- Step 3: Launch the orchestrator as fpv_bot ---
echo ""
echo "=== [3/3] Starting lobby orchestrator ==="
echo "  Playlist:  $PLAYLIST"
echo "  Interval:  ${INTERVAL}s"
[[ -n "$SHUFFLE" ]] && echo "  Shuffle:   yes"
[[ -n "$PUBLIC" ]]  && echo "  Public:    yes"
[[ $HEADLESS -eq 1 ]] && echo "  Display:   headless (Xvfb)" || echo "  Display:   GUI (:0)"
echo ""

BOT_PROJECT="/home/fpv_bot/procedural-fpv"

if [[ $HEADLESS -eq 1 ]]; then
    DISPLAY_ENV="DISPLAY=:99"
    GUI_FLAG=""
else
    DISPLAY_ENV="DISPLAY=:0"
    GUI_FLAG="--gui"
fi

sudo -u fpv_bot -H env "$DISPLAY_ENV" bash -c "
    cd '$BOT_PROJECT' &&
    python3 orchestrator/run_headless_lobby.py \
        --playlist '$PLAYLIST' \
        --interval '$INTERVAL' \
        $GUI_FLAG $SHUFFLE $PUBLIC
"
