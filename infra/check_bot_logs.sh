#!/bin/bash
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# check_bot_logs.sh - Copies bot log files to the project directory for inspection.

echo "=== Copying Bot Logs ==="
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/launch_args_log.txt $REPO_DIR/bot_launch_args_log.txt 2>/dev/null || echo "launch_args_log.txt not found"
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/game_stderr.txt $REPO_DIR/bot_game_stderr.txt 2>/dev/null || echo "game_stderr.txt not found"
sudo cp /home/fpv_bot/.config/unity3d/"LuGus Studios"/Liftoff/Player.log $REPO_DIR/bot_player_log.txt 2>/dev/null || echo "Player.log not found"
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/launch.sh $REPO_DIR/bot_launch_sh.txt 2>/dev/null || echo "launch.sh not found"
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/BepInEx/LogOutput.log $REPO_DIR/bot_bepinex_log.txt 2>/dev/null || echo "BepInEx LogOutput.log not found"

sudo chown "$(id -un):$(id -gn)" $REPO_DIR/bot_* 2>/dev/null || true
echo "Done! Logs copied to project directory."
