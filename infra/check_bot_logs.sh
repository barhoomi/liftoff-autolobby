#!/bin/bash
# check_bot_logs.sh - Copies bot log files to the project directory for inspection.

echo "=== Copying Bot Logs ==="
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/launch_args_log.txt /home/dev-user/Projects/procedural-fpv/bot_launch_args_log.txt 2>/dev/null || echo "launch_args_log.txt not found"
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/game_stderr.txt /home/dev-user/Projects/procedural-fpv/bot_game_stderr.txt 2>/dev/null || echo "game_stderr.txt not found"
sudo cp /home/fpv_bot/.config/unity3d/"LuGus Studios"/Liftoff/Player.log /home/dev-user/Projects/procedural-fpv/bot_player_log.txt 2>/dev/null || echo "Player.log not found"
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/launch.sh /home/dev-user/Projects/procedural-fpv/bot_launch_sh.txt 2>/dev/null || echo "launch.sh not found"
sudo cp /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/BepInEx/LogOutput.log /home/dev-user/Projects/procedural-fpv/bot_bepinex_log.txt 2>/dev/null || echo "BepInEx LogOutput.log not found"

sudo chown dev-user:dev-user /home/dev-user/Projects/procedural-fpv/bot_* 2>/dev/null || true
echo "Done! Logs copied to project directory."
