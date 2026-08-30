#!/bin/bash
# build.sh - Compiles the BepInEx C# plugin and syncs all project files/DLLs to both the primary and bot directories.
#
# This script is the local two-user dev loop for the dev machine specifically (dev
# user's Steam install + the separate fpv_bot user). LiftoffAutoLobby.csproj itself no
# longer hardcodes a Liftoff install path (build-release-pipeline.md R1) -- $(LiftoffPath)
# is an opt-in override, unset by default so CI and other machines build against the
# repo-local plugin/libs/ instead. This script is the one place that opts in, so `dotnet
# build plugin/` (no override) still works underneath as a plain, portable build; only
# this script's invocation is machine-specific.
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PRIMARY_LIFTOFF="${PRIMARY_LIFTOFF:-$HOME/.steam/debian-installation/steamapps/common/Liftoff}"

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
sudo "$REPO_ROOT/infra/setup_bot.sh"

echo "=== Build and Sync Completed Successfully! ==="
