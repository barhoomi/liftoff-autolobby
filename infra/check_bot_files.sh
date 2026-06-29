#!/bin/bash
# check_bot_files.sh - Lists files in the bot's game directory for structure verification.

echo "=== Listing Bot Liftoff Files ==="
sudo find /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/ -maxdepth 3 > /home/dev-user/Projects/procedural-fpv/bot_files_list.txt 2>/dev/null || echo "Failed to find files"
sudo chown dev-user:dev-user /home/dev-user/Projects/procedural-fpv/bot_files_list.txt 2>/dev/null || true
echo "Done! File list written to bot_files_list.txt"
