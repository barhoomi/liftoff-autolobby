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

# --- Defaults (overridden by bot_launch.conf, then by CLI flags) ---
PLAYLIST="all_official_races"
INTERVAL=90
SHUFFLE=0
PUBLIC=0
HEADLESS=0
BUILD=1
WIDTH=640
HEIGHT=480

# Load config file if present
CONF="$SCRIPT_DIR/bot_launch.conf"
if [[ -f "$CONF" ]]; then
    # shellcheck source=/dev/null
    source "$CONF"
fi

# --- Parse args (override config) ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --playlist)  PLAYLIST="$2"; shift 2 ;;
        --interval)  INTERVAL="$2"; shift 2 ;;
        --shuffle)   SHUFFLE=1; shift ;;
        --public)    PUBLIC=1; shift ;;
        --headless)  HEADLESS=1; shift ;;
        --no-build)  BUILD=0; shift ;;
        --width)     WIDTH="$2"; shift 2 ;;
        --height)    HEIGHT="$2"; shift 2 ;;
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

if pgrep -u fpv_bot -x steam > /dev/null 2>&1; then
    echo "Steam is already running as fpv_bot."
else
    echo "Starting Steam as fpv_bot..."
    sudo -u fpv_bot -H env DISPLAY="${DISPLAY:-:0}" XDG_RUNTIME_DIR=/run/user/1003 dbus-run-session /usr/games/steam &

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
[[ $SHUFFLE -eq 1 ]]  && echo "  Shuffle:   yes"
[[ $PUBLIC -eq 1 ]]   && echo "  Public:    yes"
[[ $HEADLESS -eq 1 ]] && echo "  Display:   headless (Xvfb)" || echo "  Display:   GUI (:0)"
echo "  Resolution: ${WIDTH}x${HEIGHT}"
echo ""

BOT_PROJECT="/home/fpv_bot/procedural-fpv"

DISPLAY_ENV="DISPLAY=:0"
GUI_FLAG="--gui"
if [[ $HEADLESS -eq 1 ]]; then
    DISPLAY_ENV="DISPLAY=:99"
    GUI_FLAG=""
fi

SHUFFLE_FLAG=""
[[ $SHUFFLE -eq 1 ]] && SHUFFLE_FLAG="--shuffle"

PUBLIC_FLAG=""
[[ $PUBLIC -eq 1 ]] && PUBLIC_FLAG="--public"

sudo -u fpv_bot -H env "$DISPLAY_ENV" XDG_RUNTIME_DIR=/run/user/1003 bash -c "
    cd '$BOT_PROJECT' &&
    python3 orchestrator/run_headless_lobby.py \
        --playlist '$PLAYLIST' \
        --interval '$INTERVAL' \
        --width '$WIDTH' \
        --height '$HEIGHT' \
        $GUI_FLAG $SHUFFLE_FLAG $PUBLIC_FLAG
"
