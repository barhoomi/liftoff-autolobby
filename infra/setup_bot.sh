#!/bin/bash
set -e

# setup_bot.sh - Automates the configuration and copy operations for the fpv_bot user.

echo "=== Liftoff Bot Account Setup Script ==="

# Runs under sudo (see build.sh), so the dev user's home comes from SUDO_USER, and the
# project dir from this script's own location.
DEV_HOME="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"
SRC_GAME_DIR="${SRC_GAME_DIR:-$DEV_HOME/.steam/debian-installation/steamapps/common/Liftoff}"
DEST_GAME_DIR="/home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff"
SRC_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_PROJECT_DIR="/home/fpv_bot/procedural-fpv"

# 1. Check directories
if [ ! -d "$SRC_GAME_DIR" ]; then
    echo "ERROR: Main Liftoff game directory not found at $SRC_GAME_DIR"
    exit 1
fi

if [ ! -d "$DEST_GAME_DIR" ]; then
    echo "ERROR: Bot Liftoff game directory not found at $DEST_GAME_DIR"
    echo "Please ensure you have run the steamcmd command to download the game first!"
    exit 1
fi

# 2. Copy mod files to bot game directory
echo "Copying BepInEx and launcher files..."
sudo cp -r "$SRC_GAME_DIR/BepInEx" "$DEST_GAME_DIR/"
sudo cp -r "$SRC_GAME_DIR/doorstop_libs" "$DEST_GAME_DIR/"
sudo cp "$SRC_GAME_DIR/launch.sh" "$DEST_GAME_DIR/"
sudo cp "$SRC_GAME_DIR/run_bepinex.sh" "$DEST_GAME_DIR/"
sudo cp "$SRC_GAME_DIR/steam_appid.txt" "$DEST_GAME_DIR/"

# 3. Copy project files
echo "Copying project scripts to bot home directory..."
if [ -d "$DEST_PROJECT_DIR" ]; then
    echo "Removing existing bot project directory..."
    sudo rm -rf "$DEST_PROJECT_DIR"
fi
sudo cp -r "$SRC_PROJECT_DIR" "$DEST_PROJECT_DIR"

# 3.5 Copy bot nickname config to both BepInEx plugins directories
cp "$SRC_PROJECT_DIR/bot_nickname.txt" "$SRC_GAME_DIR/BepInEx/plugins/" 2>/dev/null || true
sudo cp "$SRC_PROJECT_DIR/bot_nickname.txt" "$DEST_GAME_DIR/BepInEx/plugins/" 2>/dev/null || true

# 4. Fix permissions
echo "Setting owner permissions to fpv_bot..."
sudo chown -R fpv_bot:fpv_bot "$DEST_GAME_DIR"
sudo chown -R fpv_bot:fpv_bot "$DEST_PROJECT_DIR"
sudo chmod +x "$DEST_GAME_DIR"/*.sh

# 5. Correct the liftoff_path configuration
echo "Updating bot config to point to new executable path..."
sudo python3 -c "
import json
path = '$DEST_PROJECT_DIR/lobby_config.json'
with open(path, 'r') as f:
    data = json.load(f)
data['liftoff_path'] = '$DEST_GAME_DIR/Liftoff.x86_64'
with open(path, 'w') as f:
    json.dump(data, f, indent=4)
"

# 6. Copy bot Liftoff Pro credentials to game data directory (primary read location)
# The game checks Liftoff_Data/Credentials/ first; without this file it falls back
# to session discovery which can inherit the host user's active Liftoff Pro session.
#
# IMPORTANT: After a successful login the game refreshes the token and writes it to
# DEST (the game data dir). We only overwrite DEST from SRC if SRC is NEWER — this
# prevents setup_bot.sh from putting a stale token back on top of a fresh one.
BOT_CREDENTIALS_SRC="/home/fpv_bot/.config/unity3d/LuGus Studios/Liftoff/Credentials/Credentials.xml"
BOT_CREDENTIALS_DEST="$DEST_GAME_DIR/Liftoff_Data/Credentials/Credentials.xml"
if [ -f "$BOT_CREDENTIALS_SRC" ]; then
    sudo mkdir -p "$DEST_GAME_DIR/Liftoff_Data/Credentials"
    # Only overwrite if source is newer than destination (preserves fresh token after login)
    sudo cp -u "$BOT_CREDENTIALS_SRC" "$BOT_CREDENTIALS_DEST"
    echo "Copied bot Liftoff Pro credentials to game data directory."
else
    echo "WARNING: Bot credentials not found at $BOT_CREDENTIALS_SRC — bot may log in as wrong account."
fi

# 7. Optimize graphics settings for low resource usage
echo "Optimizing graphics settings for low resource usage (30 FPS limit, muted sound, disabled shadows/post-processing)..."
sudo mkdir -p "/home/fpv_bot/.config/unity3d/LuGus Studios/Liftoff/Config"

# Write optimized System.xml
sudo tee "/home/fpv_bot/.config/unity3d/LuGus Studios/Liftoff/Config/System.xml" > /dev/null << 'EOF'
<?xml version="1.0" encoding="UTF-8" ?>
<Config>
	<ShowStartupGuide>False</ShowStartupGuide>
	<InformAboutDragBakedSkin>False</InformAboutDragBakedSkin>
	<FieldOfView>109.584</FieldOfView>
	<PreferNightFeverEnvironments>True</PreferNightFeverEnvironments>
	<PreferSlipstreamAssets>False</PreferSlipstreamAssets>
	<VolumeMusic>0</VolumeMusic>
	<VolumeMaster>0</VolumeMaster>
	<OSDDefault>True</OSDDefault>
	<DontShowPerformancePopupAgain>True</DontShowPerformancePopupAgain>
	<ResolutionWidth>1280</ResolutionWidth>
	<ResolutionHeight>720</ResolutionHeight>
	<QualitySetting>0</QualitySetting>
	<ShadowDistance>0</ShadowDistance>
	<VSyncMode>0</VSyncMode>
	<UseCustomMusicPlaylist>False</UseCustomMusicPlaylist>
	<CustomMusicPlaylistPath></CustomMusicPlaylistPath>
	<Fisheye>False</Fisheye>
	<RefreshRateNumerator>60</RefreshRateNumerator>
	<RefreshRateDenominator>1</RefreshRateDenominator>
	<ShowGameTriggers>False</ShowGameTriggers>
	<ShowFestivityItems>False</ShowFestivityItems>
	<ShowFlyCage>False</ShowFlyCage>
	<ShowRaceLines>False</ShowRaceLines>
	<FullscreenMode>1</FullscreenMode>
	<LimitFramerate>True</LimitFramerate>
	<FramerateLimit>30</FramerateLimit>
	<ShowRaceGateTriggers>True</ShowRaceGateTriggers>
	<Motion_Blur_On>False</Motion_Blur_On>
	<UseVoiceChat>False</UseVoiceChat>
	<AmplifyBloom.AmplifyBloomEffect_On>False</AmplifyBloom.AmplifyBloomEffect_On>
	<AmplifyOcclusionEffect_On>False</AmplifyOcclusionEffect_On>
	<Depth_Of_Field_On>False</Depth_Of_Field_On>
	<Anti_Aliasing_On>False</Anti_Aliasing_On>
	<FisheyeLetterbox>False</FisheyeLetterbox>
	<FisheyeEnabled>False</FisheyeEnabled>
	<AcceptedDjiFpvEndUserAgreement>True</AcceptedDjiFpvEndUserAgreement>
</Config>
EOF

# Write optimized prefs file
sudo tee "/home/fpv_bot/.config/unity3d/LuGus Studios/Liftoff/prefs" > /dev/null << 'EOF'
<unity_prefs version_major="1" version_minor="1">
	<pref name="Screenmanager Fullscreen mode" type="int">0</pref>
	<pref name="Screenmanager Fullscreen mode Default" type="int">0</pref>
	<pref name="Screenmanager Resolution Height" type="int">720</pref>
	<pref name="Screenmanager Resolution Height Default" type="int">768</pref>
	<pref name="Screenmanager Resolution Use Native" type="int">0</pref>
	<pref name="Screenmanager Resolution Use Native Default" type="int">1</pref>
	<pref name="Screenmanager Resolution Width" type="int">1280</pref>
	<pref name="Screenmanager Resolution Width Default" type="int">1024</pref>
	<pref name="Screenmanager Window Position X" type="int">0</pref>
	<pref name="Screenmanager Window Position Y" type="int">0</pref>
	<pref name="UnityGraphicsQuality" type="int">0</pref>
	<pref name="UnitySelectMonitor" type="int">0</pref>
</unity_prefs>
EOF

# Fix ownership of the config files
sudo chown -R fpv_bot:fpv_bot "/home/fpv_bot/.config/unity3d"

# 8. Set up XDG_RUNTIME_DIR for fpv_bot
echo "Setting up XDG_RUNTIME_DIR for fpv_bot (UID 1003)..."
sudo mkdir -p /run/user/1003
sudo chown fpv_bot:fpv_bot /run/user/1003
sudo chmod 700 /run/user/1003

echo "=== Setup Completed Successfully! ==="
echo ""
echo "To launch your bot lobby now:"
echo "1) Enable graphical display sharing for the bot user:"
echo "   xhost +SI:localuser:fpv_bot"
echo ""
echo "2) Start the graphical Steam client logged into fpv_bot:"
echo "   sudo -u fpv_bot -H env XDG_RUNTIME_DIR=/run/user/1003 dbus-run-session /usr/games/steam &"
echo ""
echo "3) Navigate to the bot's project folder and start the lobby:"
echo "   cd /home/fpv_bot/procedural-fpv/"
echo "   sudo -u fpv_bot -H env DISPLAY=:0 python3 orchestrator/run_headless_lobby.py --playlist all_official_races --interval 90 --gui"
echo ""
