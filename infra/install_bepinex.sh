#!/bin/bash
# Script to install BepInEx and UnityExplorer into Liftoff

set -e

# Default Liftoff paths
DEFAULT_LIFTOFF_PATH="$HOME/.steam/debian-installation/steamapps/common/Liftoff"

# Use command line path if provided
LIFTOFF_DIR="${1:-$DEFAULT_LIFTOFF_PATH}"

if [ ! -d "$LIFTOFF_DIR" ]; then
    echo "ERROR: Liftoff directory not found at: $LIFTOFF_DIR"
    echo "Usage: ./install_bepinex.sh [path/to/Liftoff]"
    exit 1
fi

echo "[Installer] Found Liftoff directory at: $LIFTOFF_DIR"

# 1. Download BepInEx Unix v5.4.22.0
BP_ZIP="BepInEx_unix_5.4.22.0.zip"
BP_URL="https://github.com/BepInEx/BepInEx/releases/download/v5.4.22/$BP_ZIP"

if [ ! -f "/tmp/$BP_ZIP" ]; then
    echo "[Installer] Downloading BepInEx Unix..."
    curl -L "$BP_URL" -o "/tmp/$BP_ZIP"
fi

echo "[Installer] Extracting BepInEx to game folder..."
unzip -o "/tmp/$BP_ZIP" -d "$LIFTOFF_DIR"

# 2. Download UnityExplorer for BepInEx 5 Mono v4.9.0
UE_ZIP="UnityExplorer.BepInEx5.Mono.zip"
UE_URL="https://github.com/sinai-dev/UnityExplorer/releases/download/v4.9.0/$UE_ZIP"

if [ ! -f "/tmp/$UE_ZIP" ]; then
    echo "[Installer] Downloading UnityExplorer..."
    curl -L "$UE_URL" -o "/tmp/$UE_ZIP"
fi

echo "[Installer] Extracting UnityExplorer to game folder..."
unzip -o "/tmp/$UE_ZIP" -d "$LIFTOFF_DIR"

# 3. Configure execution permissions
chmod +x "$LIFTOFF_DIR/run_bepinex.sh"

echo ""
echo "================================================================================"
echo "SUCCESS: BepInEx & UnityExplorer have been successfully installed!"
echo "================================================================================"
echo "To run the game with BepInEx enabled:"
echo "1. In your shell, run: $LIFTOFF_DIR/run_bepinex.sh"
echo "2. Or in Steam, set the launch options for Liftoff to:"
echo "   $LIFTOFF_DIR/run_bepinex.sh %command%"
echo "================================================================================"
echo ""
