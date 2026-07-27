#!/bin/bash
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# check_bot_files.sh - Lists files in the bot's game directory for structure verification.

echo "=== Listing Bot Liftoff Files ==="
sudo find /home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff/ -maxdepth 3 > $REPO_DIR/bot_files_list.txt 2>/dev/null || echo "Failed to find files"
sudo chown "$(id -un):$(id -gn)" $REPO_DIR/bot_files_list.txt 2>/dev/null || true
echo "Done! File list written to bot_files_list.txt"
