#!/bin/bash
# check_bot_project_dir.sh - Lists files in the bot's project folder to see why it appears empty.

echo "=== Listing Bot Project Directory ==="
sudo ls -la /home/fpv_bot/procedural-fpv/ > /home/dev-user/Projects/procedural-fpv/bot_project_files.txt 2>&1 || echo "ls failed"
sudo chown dev-user:dev-user /home/dev-user/Projects/procedural-fpv/bot_project_files.txt 2>/dev/null || true
echo "Done! File list written to bot_project_files.txt"
