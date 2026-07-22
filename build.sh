#!/bin/bash
# build.sh - Compiles the BepInEx C# plugin and syncs all project files/DLLs to both the primary and bot directories.
#
# This script is the local two-user dev loop for dev-user's machine specifically (see
# AGENTS.md "Two-user setup"). LiftoffAutoLobby.csproj itself no longer hardcodes a
# Liftoff install path (build-release-pipeline.md R1) -- $(LiftoffPath) is an opt-in
# override, unset by default so CI and other machines build against the repo-local
# plugin/libs/ instead. This script is the one place that opts in, so `dotnet build
# plugin/` (no override) still works underneath as a plain, portable build; only this
# script's invocation is dev-user-specific.
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

PRIMARY_LIFTOFF="/home/dev-user/.steam/debian-installation/steamapps/common/Liftoff"

echo "=== 1. Compiling BepInEx C# Plugin ==="
dotnet build plugin/ -c Debug -p:LiftoffPath="$PRIMARY_LIFTOFF"

echo "=== 2. Syncing to Primary User BepInEx ==="
# csproj's PostBuild target already copied the DLL here (it fires whenever LiftoffPath
# is set and exists, see LiftoffAutoLobby.csproj); this step is kept as an explicit,
# visible confirmation of that and as a fallback if PostBuild's condition ever changes.
PRIMARY_PLUGINS="$PRIMARY_LIFTOFF/BepInEx/plugins"
if [ -d "$PRIMARY_PLUGINS" ]; then
    cp plugin/bin/Debug/LiftoffAutoLobby.dll "$PRIMARY_PLUGINS/"
    echo "Copied DLL to primary user plugins."
else
    echo "Warning: Primary user plugins directory not found."
fi

echo "=== 3. Syncing to Bot Account ==="
sudo /home/dev-user/Projects/procedural-fpv/infra/setup_bot.sh

echo "=== Build and Sync Completed Successfully! ==="
