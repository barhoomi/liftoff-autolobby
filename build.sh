#!/bin/bash
# build.sh - Compiles the BepInEx C# plugin and syncs all project files/DLLs to both the primary and bot directories.
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

echo "=== 1. Compiling BepInEx C# Plugin ==="
dotnet build plugin/ -c Debug

echo "=== 2. Syncing to Primary User BepInEx ==="
PRIMARY_PLUGINS="/home/dev-user/.steam/debian-installation/steamapps/common/Liftoff/BepInEx/plugins"
if [ -d "$PRIMARY_PLUGINS" ]; then
    cp plugin/bin/Debug/LiftoffAutoLobby.dll "$PRIMARY_PLUGINS/"
    echo "Copied DLL to primary user plugins."
else
    echo "Warning: Primary user plugins directory not found."
fi

echo "=== 3. Syncing to Bot Account ==="
sudo /home/dev-user/Projects/procedural-fpv/infra/setup_bot.sh

echo "=== Build and Sync Completed Successfully! ==="
