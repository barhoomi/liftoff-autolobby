#!/bin/bash
# Automatically update the repository and execute the track generator

# Exit on any error
set -e

# Get the directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# 1. Pull the latest changes from Git
echo "[Runner] Checking for updates from Git..."
git fetch origin
git reset --hard origin/main

# 2. Run the track generator using the virtual environment python
echo "[Runner] Running generator script..."
./venv/bin/python main.py "$@"
