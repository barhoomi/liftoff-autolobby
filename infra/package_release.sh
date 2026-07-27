#!/bin/bash
# infra/package_release.sh - build the plugin and assemble the two release zips
# (player + server) described in docs/features/doing/build-release-pipeline.md
# ("Package layout"). Used by .github/workflows/release.yml on a `v*` tag push, and
# runnable locally the same way.
#
# Usage: bash infra/package_release.sh
#
# Requires plugin/libs/ to already contain the game reference assemblies (restored from
# the private source -- see that doc's "Operator runbook"), OR $LiftoffPath pointed at a
# local Liftoff install for a manual/dev run.
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." &> /dev/null && pwd )"
cd "$REPO_ROOT"

VERSION="$(grep -oP '(?<=<Version>)[^<]+' Directory.Build.props)"
if [ -z "$VERSION" ]; then
    echo "Could not read <Version> from Directory.Build.props" >&2
    exit 1
fi
echo "=== Packaging LiftoffAutoLobby v$VERSION ==="

echo "--- Building plugin (Release) ---"
if [ -n "$LiftoffPath" ]; then
    dotnet build plugin/ -c Release -p:LiftoffPath="$LiftoffPath"
else
    dotnet build plugin/ -c Release
fi

DLL="plugin/bin/Release/LiftoffAutoLobby.dll"
if [ ! -f "$DLL" ]; then
    echo "Expected build output not found: $DLL" >&2
    exit 1
fi

DIST="dist"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "--- Assembling player package ---"
PLAYER_STAGE="$DIST/player-stage"
mkdir -p "$PLAYER_STAGE/BepInEx/plugins/LiftoffAutoLobby"
cp "$DLL" "$PLAYER_STAGE/BepInEx/plugins/LiftoffAutoLobby/LiftoffAutoLobby.dll"
cp packaging/player/tracks_to_rotate.example.txt "$PLAYER_STAGE/BepInEx/plugins/LiftoffAutoLobby/tracks_to_rotate.txt"
cp packaging/player/README.md "$PLAYER_STAGE/README.md"
PLAYER_ZIP="$REPO_ROOT/$DIST/liftoff-autolobby-player-v$VERSION.zip"
( cd "$PLAYER_STAGE" && zip -r -q "$PLAYER_ZIP" . )
rm -rf "$PLAYER_STAGE"
echo "Wrote $PLAYER_ZIP"

echo "--- Assembling server package ---"
SERVER_STAGE="$DIST/server-stage"
mkdir -p "$SERVER_STAGE/BepInEx/plugins/LiftoffAutoLobby"
cp "$DLL" "$SERVER_STAGE/BepInEx/plugins/LiftoffAutoLobby/LiftoffAutoLobby.dll"
cp -r orchestrator "$SERVER_STAGE/orchestrator"
cp -r generator "$SERVER_STAGE/generator"
cp -r trackcheck "$SERVER_STAGE/trackcheck"
cp -r infra "$SERVER_STAGE/infra"
cp Dockerfile docker-compose.yml "$SERVER_STAGE/"
mkdir -p "$SERVER_STAGE/config"
cp config/lobby_config.json config/playlists.json "$SERVER_STAGE/config/"
cp packaging/server/README-server.md "$SERVER_STAGE/README-server.md"
# Strip dev cruft that shouldn't ship in a release zip.
find "$SERVER_STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$SERVER_STAGE" -name "*.pyc" -delete
SERVER_ZIP="$REPO_ROOT/$DIST/liftoff-autolobby-server-v$VERSION.zip"
( cd "$SERVER_STAGE" && zip -r -q "$SERVER_ZIP" . )
rm -rf "$SERVER_STAGE"
echo "Wrote $SERVER_ZIP"

echo "=== Done ==="
ls -la "$DIST"
