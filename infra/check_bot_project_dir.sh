#!/bin/bash
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# check_bot_project_dir.sh - Lists files in the bot's project folder to see why it appears empty.

echo "=== Listing Bot Project Directory ==="
sudo ls -la /home/fpv_bot/procedural-fpv/ > $REPO_DIR/bot_project_files.txt 2>&1 || echo "ls failed"
sudo chown "$(id -un):$(id -gn)" $REPO_DIR/bot_project_files.txt 2>/dev/null || true
echo "Done! File list written to bot_project_files.txt"
