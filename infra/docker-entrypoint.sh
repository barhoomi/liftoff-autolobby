#!/bin/bash
# docker-entrypoint.sh — container startup for the procedural-fpv bot.
#
# Reproduces, inside the container, the same flow run_bot.sh / setup_bot.sh drive on the
# host (see AGENTS.md): steamcmd installs/updates the paid game into a persistent volume,
# BepInEx gets deployed into it, the plugin is compiled against that install's Managed
# DLLs, a real (graphical, Xvfb-hosted) Steam client is started and logged in — the game
# binary needs a live Steam client process for its Steamworks IPC pipe, not just steamcmd
# (see the "Spec conflict" section in docs/features/doing/docker-container.md) — and only
# then is the Python orchestrator handed off to.
#
# Design: root does one-time volume/ownership prep, then re-execs itself as the
# unprivileged `botuser` (Steam refuses to run as root) for everything else, ending in an
# `exec` into the orchestrator so it becomes the container's foreground process.
set -euo pipefail

# The `steamcmd` and `steam-installer` apt packages install their binaries under
# /usr/games (Debian convention for games). That directory is only on PATH for
# interactive login shells (via /etc/profile) -- not for this script's exec context,
# nor for `docker run --entrypoint steamcmd ...`. Add it explicitly so the bare
# `steamcmd` (below) and `steam` (later, for the graphical client) invocations resolve.
export PATH="/usr/games:$PATH"

log()   { echo "[entrypoint] $*"; }
err()   { echo "[entrypoint] ERROR: $*" >&2; }
fatal() { err "$*"; exit 1; }

STEAM_DIR="${STEAM_DIR:-/steam}"
LOG_DIR="${LOG_DIR:-/logs}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
PROJECT_DIR="${PROJECT_DIR:-/app}"
BOT_USER="botuser"
BOT_UID=1000
DISPLAY_NUM="${DISPLAY:-:99}"

# ============================================================================
# Stage 1 (root): volume prep, then drop privileges. Steam hard-refuses to run
# as root, and there is no other reason for the rest of this script to run as
# root.
# ============================================================================
if [[ "$(id -u)" -eq 0 ]]; then
    log "=== procedural-fpv bot container starting (root prep stage) ==="

    for d in "$STEAM_DIR" "$LOG_DIR" "$CONFIG_DIR"; do
        mkdir -p "$d"
        owner="$(stat -c '%U' "$d" 2>/dev/null || echo '?')"
        if [[ "$owner" != "$BOT_USER" ]]; then
            log "Fixing ownership of $d (was '$owner')..."
            chown -R "$BOT_USER:$BOT_USER" "$d"
        fi
    done
    mkdir -p /run/user/$BOT_UID
    chown "$BOT_USER:$BOT_USER" /run/user/$BOT_UID
    chmod 700 /run/user/$BOT_UID

    log "Handing off to unprivileged user '$BOT_USER'..."
    exec runuser -u "$BOT_USER" -- env \
        HOME="$STEAM_DIR" \
        DISPLAY="$DISPLAY_NUM" \
        XDG_RUNTIME_DIR="/run/user/$BOT_UID" \
        STEAM_DIR="$STEAM_DIR" LOG_DIR="$LOG_DIR" CONFIG_DIR="$CONFIG_DIR" PROJECT_DIR="$PROJECT_DIR" \
        FPV_LOG_DIR="$LOG_DIR" \
        "$0" "$@"
fi

# ============================================================================
# Stage 2 (botuser): everything else.
# ============================================================================
: "${STEAM_ACCOUNT:?STEAM_ACCOUNT env var is required -- the Steam login name whose \
session was primed into the /steam volume. See the human-dependency section of \
docs/features/doing/docker-container.md for the one-time interactive login step.}"

LIFTOFF_APP_ID="410340"
LIFTOFF_INSTALL_DIR="${LIFTOFF_INSTALL_DIR:-$STEAM_DIR/Liftoff}"

PLAYLIST="${PLAYLIST:-all_official_races}"
INTERVAL="${INTERVAL:-90}"
SHUFFLE="${SHUFFLE:-false}"
PUBLIC="${PUBLIC:-false}"
AUTO_START="${AUTO_START:-false}"
LOBBY_NAME="${LOBBY_NAME:-Procedural Loop Room}"
WIDTH="${WIDTH:-640}"
HEIGHT="${HEIGHT:-480}"
MAX_PLAYERS="${MAX_PLAYERS:-}"
STEAMCMD_TIMEOUT="${STEAMCMD_TIMEOUT:-900}"

log "STEAM_ACCOUNT=$STEAM_ACCOUNT  LIFTOFF_INSTALL_DIR=$LIFTOFF_INSTALL_DIR  DISPLAY=$DISPLAY_NUM"

# ---------- seed persistent config from image defaults on first run ----------
# The orchestrator (run_headless_lobby.py / gather_tracks.py) only ever reads
# <repo_root>/<file> -- there's no env-var override wired into that code today, and
# adding one is out of scope for this task (see feature doc "spec conflicts" section).
# Instead: seed /config from the image defaults on first run, then symlink the repo-root
# copies to /config so edits made via the volume survive container recreation, matching
# what the /config volume is for per the spec.
for f in lobby_config.json playlists.json master_tracks_list.json; do
    # master_tracks_list.json ships in neither the repo nor the image -- it's generated at
    # runtime by gather_tracks.py's gather_tracks_and_races() (see AGENTS.md: it's a local,
    # gitignored artifact). Only seed from an image default when one actually exists; the
    # symlink still needs creating either way so the file lands on /config once generated.
    if [[ ! -f "$CONFIG_DIR/$f" && -f "$PROJECT_DIR/$f" ]]; then
        log "Seeding $CONFIG_DIR/$f from image default."
        cp "$PROJECT_DIR/$f" "$CONFIG_DIR/$f"
    fi
    rm -f "$PROJECT_DIR/$f"
    ln -sf "$CONFIG_DIR/$f" "$PROJECT_DIR/$f"
done

# lobby_config.json's liftoff_path/display must point at *this* container's install dir
# and display, not whatever the image's baked-in default says. Rewrite in place
# (idempotent) -- lobby_name is intentionally left alone since run_headless_lobby.py's
# --lobby-name CLI flag (passed below, from $LOBBY_NAME) always wins over the config
# value anyway.
python3 - "$CONFIG_DIR/lobby_config.json" "$LIFTOFF_INSTALL_DIR/Liftoff.x86_64" "$DISPLAY_NUM" <<'PYEOF'
import json, sys
path, liftoff_path, display = sys.argv[1:4]
with open(path) as f:
    data = json.load(f)
data["liftoff_path"] = liftoff_path
data["display"] = display
with open(path, "w") as f:
    json.dump(data, f, indent=4)
PYEOF

# ---------- steamcmd install/update ----------
# No separate "is a login cached?" pre-check: an earlier version of this script looked for
# loginusers.vdf under $STEAM_DIR/.steam/... or $STEAM_DIR/.local/share/Steam/..., but this
# Debian steamcmd package never writes that file at all -- its login state lives in
# $STEAM_DIR/Steam/config/config.vdf (loginusers.vdf is written by the *graphical* Steam
# client, which hasn't run yet at this point in the script). Caught live: the pre-check
# always reported "no cached login" even right after a successful prime. Rather than chase
# the right path/format for a second, redundant check, treat the real `steamcmd +login`
# call below as the single source of truth for whether credentials are cached (AGENTS.md
# rule 4) -- it already runs under `timeout` with `+@NoPromptForPassword 1` (so a missing/
# expired login fails fast instead of hanging on a prompt with no TTY attached) and already
# classifies the failure below.
print_priming_instructions() {
    cat >&2 <<EOF

================================================================================
Liftoff is a paid game -- steamcmd/Steam cannot authenticate anonymously. A
human must prime the credential cache ONCE, interactively (Steam Guard code
entry included), against this exact volume before the container can run
unattended:

    docker run -it --rm \\
        -v <same steam volume>:/steam \\
        -u botuser -e HOME=/steam \\
        --entrypoint /usr/games/steamcmd \\
        <this image> \\
        +force_install_dir $LIFTOFF_INSTALL_DIR +login $STEAM_ACCOUNT +quit

Enter the password and Steam Guard code when prompted, then 'quit'. This only
needs to be done once (or again after the token expires/is revoked) --
subsequent container starts reuse the cached session under $STEAM_DIR/Steam/...
(shared by steamcmd and the full graphical Steam client started later in this
script; both need it).
================================================================================

EOF
}

log "Running steamcmd install/update for app $LIFTOFF_APP_ID -> $LIFTOFF_INSTALL_DIR (timeout ${STEAMCMD_TIMEOUT}s)..."
set +e
STEAMCMD_OUT="$(timeout "$STEAMCMD_TIMEOUT" steamcmd \
    +@NoPromptForPassword 1 \
    +force_install_dir "$LIFTOFF_INSTALL_DIR" \
    +login "$STEAM_ACCOUNT" \
    +app_update "$LIFTOFF_APP_ID" validate \
    +quit 2>&1)"
STEAMCMD_STATUS=$?
set -e
echo "$STEAMCMD_OUT" | tail -n 60

# Degraded-mode boot (see docker-container.md "Eleventh incident"): the steamcmd token and
# the graphical client's token live in separate stores that MUTUALLY INVALIDATE each other
# -- re-priming one revokes the other, so sequential manual priming can never get both
# valid at once. steamcmd's token is only needed to install/update the game; the game
# itself only needs the graphical client's token (checked much later, at the readiness
# gate) for SteamAPI_Init. So a dead steamcmd token should not be fatal when a usable
# install already exists -- fail loud, skip the update check for this boot, and continue.
# Still hard-fail with no install: there is nothing to run in that case.
usable_install_present() {
    [[ -x "$LIFTOFF_INSTALL_DIR/Liftoff.x86_64" ]]
}

if [[ $STEAMCMD_STATUS -eq 124 ]]; then
    if usable_install_present; then
        log "WARNING: DEGRADED BOOT -- steamcmd timed out after ${STEAMCMD_TIMEOUT}s (likely stuck on an interactive prompt: expired/invalid cached token, a new-device Steam Guard re-check, or a rate limit); existing install present at $LIFTOFF_INSTALL_DIR, skipping update check and continuing. Re-prime steamcmd credentials soon -- see the priming instructions below -- so this boot doesn't run a version-drifted game client indefinitely."
        print_priming_instructions
    else
        print_priming_instructions
        fatal "steamcmd timed out after ${STEAMCMD_TIMEOUT}s -- almost certainly stuck on an interactive prompt (expired/invalid cached token, a new-device Steam Guard re-check, or a rate limit), and no usable install exists at $LIFTOFF_INSTALL_DIR to fall back on. Re-prime credentials as shown above. Not retrying automatically -- fix this before restarting the container."
    fi
elif [[ $STEAMCMD_STATUS -ne 0 ]]; then
    if echo "$STEAMCMD_OUT" | grep -qiE "Invalid Password|Login Failure|Two-factor|Steam Guard|Access Denied|InvalidSignature|Rate Limit"; then
        if usable_install_present; then
            log "WARNING: DEGRADED BOOT -- steamcmd auth failed (cached token expired, revoked, or needs re-verification; see steamcmd output above); existing install present at $LIFTOFF_INSTALL_DIR, skipping update check and continuing. Re-prime steamcmd credentials soon -- see the priming instructions below -- so this boot doesn't run a version-drifted game client indefinitely."
            print_priming_instructions
        else
            print_priming_instructions
            fatal "steamcmd login failed -- the cached Steam token has expired, was revoked, or needs re-verification (see steamcmd output above), and no usable install exists at $LIFTOFF_INSTALL_DIR to fall back on. Re-prime credentials as shown above. Not retrying automatically."
        fi
    elif usable_install_present; then
        log "WARNING: DEGRADED BOOT -- steamcmd exited with status $STEAMCMD_STATUS (see output above); existing install present at $LIFTOFF_INSTALL_DIR, skipping update check and continuing. Investigate the steamcmd failure soon so this boot doesn't run a version-drifted game client indefinitely."
    else
        fatal "steamcmd exited with status $STEAMCMD_STATUS (see output above), and no usable install exists at $LIFTOFF_INSTALL_DIR to fall back on. Not retrying automatically."
    fi
else
    log "steamcmd install/update complete."
fi

# ---------- BepInEx ----------
if [[ ! -d "$LIFTOFF_INSTALL_DIR/BepInEx/core" ]]; then
    log "BepInEx not found in the install -- installing..."
    bash "$PROJECT_DIR/infra/install_bepinex.sh" "$LIFTOFF_INSTALL_DIR"
else
    log "BepInEx already present."
fi
mkdir -p "$LIFTOFF_INSTALL_DIR/BepInEx/plugins"

if [[ -f "$PROJECT_DIR/bot_nickname.txt" ]]; then
    cp "$PROJECT_DIR/bot_nickname.txt" "$LIFTOFF_INSTALL_DIR/BepInEx/plugins/bot_nickname.txt"
fi

# ---------- admin IDs (server-mode chat command authorization) ----------
# LoadAdminIds() in Plugin.cs (plugin/Plugin.cs:212) reads BepInEx/plugins/admin_ids.txt --
# one Photon user ID per line, blank lines and '#'-prefixed comment lines ignored -- and
# treats a missing/empty file as "no admins configured" (a deliberate fail-closed default,
# not a bug: see plugin/Plugin.cs:232's IsAdmin()). This container never wrote that file at
# all, so every admin-only chat command (/interval, /kick, ...) silently no-oped for every
# user, including the operator -- see docs/features/doing/container-seed-admin-ids.md.
#
# Two operator-facing mechanisms, tried in this precedence order (same env-over-mounted-file
# shape as resolve_log_dir()'s env-over-config chain, docs/features/done/structured-logging.md):
#   1. ADMIN_IDS env var -- comma and/or newline separated Photon user IDs. .env-friendly,
#      matches how the rest of this script's config arrives via docker-compose.yml's
#      env_file: .env.
#   2. A file mounted at $CONFIG_DIR/admin_ids.txt (e.g. bind-mount your own admin_ids.txt to
#      /config/admin_ids.txt:ro) -- copied through verbatim so the host's comment header /
#      one-per-line format (see the feature doc's evidence section) works unchanged.
# Regenerated every boot (idempotent overwrite, matching the Pro-credentials sync below) so
# this script is the single source of truth for the destination file (AGENTS.md rule 4) --
# no stale admin list can survive an operator changing/clearing the config. An empty/unset
# ADMIN_IDS with no mounted file is NOT an error: it reproduces today's fail-closed default
# (no admin_ids.txt -> no admins) and is logged loudly so this doesn't require docker exec
# spelunking to notice again.
ADMIN_IDS_DEST="$LIFTOFF_INSTALL_DIR/BepInEx/plugins/admin_ids.txt"
if [[ -n "${ADMIN_IDS:-}" ]]; then
    : > "$ADMIN_IDS_DEST"
    ADMIN_ID_COUNT=0
    while IFS= read -r id; do
        id="$(echo -n "$id" | tr -d '[:space:]')"
        [[ -z "$id" ]] && continue
        echo "$id" >> "$ADMIN_IDS_DEST"
        ADMIN_ID_COUNT=$((ADMIN_ID_COUNT + 1))
    done < <(echo "$ADMIN_IDS" | tr ',' '\n')
    log "Seeded $ADMIN_ID_COUNT admin ID(s) into admin_ids.txt from \$ADMIN_IDS."
elif [[ -f "$CONFIG_DIR/admin_ids.txt" ]]; then
    cp "$CONFIG_DIR/admin_ids.txt" "$ADMIN_IDS_DEST"
    MOUNTED_COUNT="$(grep -vcE '^[[:space:]]*(#|$)' "$CONFIG_DIR/admin_ids.txt" || true)"
    log "Seeded admin_ids.txt from mounted $CONFIG_DIR/admin_ids.txt ($MOUNTED_COUNT admin ID(s))."
else
    rm -f "$ADMIN_IDS_DEST"
    log "WARNING: no ADMIN_IDS env var set and no $CONFIG_DIR/admin_ids.txt mounted -- admin_ids.txt NOT written. Fail-closed: admin-only chat commands (/interval, /kick, ...) will no-op for everyone, including the operator, until one is configured. Set ADMIN_IDS in .env (comma-separated Photon user IDs) or mount a file at $CONFIG_DIR/admin_ids.txt."
fi

# ---------- build the plugin against THIS install's Managed DLLs ----------
# LiftoffPath is overridable via an MSBuild global property (-p:) without any csproj
# change -- confirmed by testing that a bogus override changes which Managed DLL paths
# the compiler reports as missing (see feature doc "verification" section).
log "Building BepInEx plugin (dotnet build, LiftoffPath override)..."
dotnet build "$PROJECT_DIR/plugin" -c Debug -p:LiftoffPath="$LIFTOFF_INSTALL_DIR" \
    || fatal "Plugin build failed against $LIFTOFF_INSTALL_DIR -- see dotnet output above. This is a build problem, not a Steam auth problem; the game install/BepInEx deploy above succeeded. Investigate before retrying."
log "Plugin build complete; LiftoffAutoLobby.dll deployed via the csproj's PostBuild copy."

# ---------- Liftoff Pro credentials + low-resource graphics defaults (first run only) ---
BOT_CREDENTIALS_SRC="$STEAM_DIR/.config/unity3d/LuGus Studios/Liftoff/Credentials/Credentials.xml"
BOT_CREDENTIALS_DEST="$LIFTOFF_INSTALL_DIR/Liftoff_Data/Credentials/Credentials.xml"
if [[ -f "$BOT_CREDENTIALS_SRC" ]]; then
    mkdir -p "$(dirname "$BOT_CREDENTIALS_DEST")"
    cp -u "$BOT_CREDENTIALS_SRC" "$BOT_CREDENTIALS_DEST"
    log "Synced Liftoff Pro credentials into the game data dir."
fi

UNITY_CFG_DIR="$STEAM_DIR/.config/unity3d/LuGus Studios/Liftoff/Config"
if [[ ! -f "$UNITY_CFG_DIR/System.xml" ]]; then
    log "Seeding low-resource graphics defaults (first run only; edit the files under $STEAM_DIR/.config/unity3d/... directly to change them later)..."
    mkdir -p "$UNITY_CFG_DIR"
    cat > "$UNITY_CFG_DIR/System.xml" <<'XMLEOF'
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
XMLEOF
    cat > "$STEAM_DIR/.config/unity3d/LuGus Studios/Liftoff/prefs" <<'PREFSEOF'
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
PREFSEOF
fi

# ---------- Xvfb ----------
log "Starting Xvfb on display $DISPLAY_NUM ..."
Xvfb "$DISPLAY_NUM" -screen 0 1280x720x24 &
XVFB_PID=$!
sleep 2
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    fatal "Xvfb failed to start on display $DISPLAY_NUM."
fi

# ---------- x11vnc (one-time interactive Steam login + observation) ----------
# No password (-nopw) is acceptable ONLY because docker-compose.yml binds the host side to
# 127.0.0.1 -- see docs/features/backlog/docker-steam-sandbox-hardening.md for the broader
# hardening pass.
log "Starting x11vnc on $DISPLAY_NUM (host: connect a VNC viewer to localhost:5900)..."
if ! x11vnc -display "$DISPLAY_NUM" -forever -shared -nopw -quiet -bg -o /tmp/x11vnc.log; then
    err "x11vnc failed to start (see /tmp/x11vnc.log) -- continuing, but the display won't be observable and a first-time Steam login cannot be performed."
fi

# ---------- graphical Steam client ----------
# The game binary calls SteamAPI_Init, which talks to a *running, logged-in Steam client*
# process over a local IPC pipe -- steamcmd (above) is a separate, short-lived content
# tool and does not provide this. This matches how the bot already runs on the host
# (run_bot.sh starts `dbus-run-session /usr/games/steam`, not steamcmd, before launching
# the game) and the documented "steamid=0 -> SteamAPI_Init False" black-screen failure
# mode when Steam isn't signed in. See the feature doc's "Spec conflict" section.
# Steam's Linux compat layer (Pressure Vessel/bubblewrap) needs container privileges Docker
# doesn't grant by default -- see docker-compose.yml's cap_add/security_opt comments for the
# escalating bwrap failures found live 2026-07-12 (namespace creation -> mount-slave
# propagation -> pivot_root). STEAM_RUNTIME=0 was tried as a way to skip the sandbox
# entirely but made things worse: steam.sh's own steam-runtime-check-requirements script
# runs regardless of that variable, and on failure prints an interactive "Press enter to
# continue:" prompt that hangs forever with no TTY attached (worse than the errors-but-
# continues behavior without it). Reverted -- fixing this via docker-compose.yml's
# cap_add/security_opt instead.
#
# LOGIN STATE (found live 2026-07-12): the graphical client's login is SEPARATE from
# steamcmd's. The steamcmd prime caches a token under $STEAM_DIR/Steam/config/config.vdf,
# but that token is machine-scoped (JWT aud:["machine"] + an encrypted ConnectCache blob)
# and the graphical client cannot use it -- it keeps its own refresh token, recorded in
# $STEAM_DIR/.steam/debian-installation/config/loginusers.vdf after a successful UI login.
# Without that, the client runs logged OUT (every steamwebhelper carries -steamid=0, the
# connection log shows [U:1:0]) and the game's SteamAPI_Init() returns False forever even
# though IsSteamRunning()=True and steamclient.so loads fine. So: a human must log in
# through the client's own UI once, via the x11vnc session started above; the client's
# token then persists in the /steam volume and later boots auto-login silently.
STEAM_CLIENT_ROOT="$STEAM_DIR/.steam/debian-installation"
LOGINUSERS_VDF="$STEAM_CLIENT_ROOT/config/loginusers.vdf"
CONNECTION_LOG="$STEAM_CLIENT_ROOT/logs/connection_log.txt"

# Does the volume hold a cached client login to even attempt? (loginusers.vdf with an
# account.) This decides silent auto-login vs. a visible login window -- it is NOT proof
# the login still works: the token can be revoked server-side and this file stays behind.
client_has_cached_login() {
    [[ -f "$LOGINUSERS_VDF" ]] && grep -q '"AccountName"' "$LOGINUSERS_VDF"
}

# Is the client ACTUALLY authenticated to Steam right now? Two independent positive signals;
# EITHER is sufficient. loginusers.vdf existence alone is NOT proof -- it fooled the gate on
# 2026-07-13 when a steamcmd password re-prime revoked the client's separate token
# ("Access Denied", see below) yet left the file behind, racing the game into a permanent
# SplashScreen SteamAPI_Init failure.
#
# Signal 1 -- a running steamwebhelper carries -steamid=<N>, N>0 (N==0 == logged OUT, bug
#   eight). Fires on the human-login UI path.
# Signal 2 (authoritative for the -silent auto-login path) -- the connection log's MOST
#   RECENT client logon completed 'OK' for a NON-ZERO steamid and was not subsequently
#   rejected. On the silent path the steamwebhelper is spawned during the connecting phase
#   and keeps -steamid=0 for the WHOLE session even after a fully successful 'OK' logon, so
#   signal 1 alone false-negatives and fatal-times-out a perfectly valid login. Found live
#   2026-07-13 (twelfth incident): connection_log showed a JWT logon 'OK' as [U:1:722984222]
#   at 12:12:03, yet every steamwebhelper stayed -steamid=0 for the full 600s readiness
#   budget, and the gate (which only had signal 1 at the time) killed a server-side-valid
#   login. The CM-level 'OK' logon -- not the CEF UI webhelper's cmdline -- is what actually
#   reflects the login the game's SteamAPI_Init depends on. Take the last of {OK-logon,
#   Access-Denied, Do-not-reconnect} in the recent tail; authenticated iff that last one is
#   the OK logon (revoked -> Access Denied is last -> FALSE; logged-out anonymous -> no
#   OK-for-non-zero line -> FALSE).
client_authenticated() {
    if pgrep -af -- '-steamid=' 2>/dev/null | grep -qE -- '-steamid=[1-9][0-9]*'; then
        return 0
    fi
    [[ -f "$CONNECTION_LOG" ]] || return 1
    tail -n 40 "$CONNECTION_LOG" 2>/dev/null \
        | grep -E "RecvMsgClientLogOnResponse\(\) : \[U:1:[1-9][0-9]*\] 'OK'|Access Denied|Do not reconnect" \
        | tail -n 1 | grep -q "'OK'"
}

# Did the client's cached-token auto-login get REJECTED server-side (revoked, not expired)?
# Signature: "Access Denied" / "Do not reconnect" in the connection log -- the client gives
# up and will not self-recover. Distinct from a slow cold boot (still logging in). Tail-only
# so a stale denial from a much-earlier boot doesn't dominate; used to pick the right fatal
# message, and (silent path only) to fail fast instead of burning the whole timeout.
client_auth_revoked() {
    [[ -f "$CONNECTION_LOG" ]] && tail -n 40 "$CONNECTION_LOG" 2>/dev/null \
        | grep -qE 'Access Denied|Do not reconnect'
}

if client_has_cached_login; then
    log "Graphical Steam client has a cached login ($LOGINUSERS_VDF) -- starting silent."
    STEAM_LOGIN_PENDING=0
    dbus-run-session -- steam -silent &
else
    STEAM_LOGIN_PENDING=1
    cat >&2 <<EOF

================================================================================
The graphical Steam client in this /steam volume has never been logged in.
(The steamcmd credential prime is NOT enough -- its token is machine-scoped and
the graphical client cannot use it.) ONE-TIME manual step:

    1. On the host, connect a VNC viewer to  localhost:5900
       (e.g.  vncviewer localhost:5900  , or Remmina -> VNC).
    2. Log in to Steam as '$STEAM_ACCOUNT' in the window that appears
       (password + Steam Guard). LEAVE "Remember me" ENABLED so the token
       is cached.
    3. That's it -- this script waits (default 30 min; STEAM_LOGIN_TIMEOUT to
       change) and continues automatically once the login lands. Future
       container starts skip this step (the token persists in the volume).
================================================================================

EOF
    log "Starting graphical Steam client WITH visible login window (no -silent)..."
    dbus-run-session -- steam &
fi

# Readiness = ALL of: (a) the steamclient.so the game will dlopen exists (on a first-ever
# cold boot Steam's self-update/first-run setup takes minutes before the
# ~/.steam/{sdk32,sdk64} symlink chain exists -- a process-existence check alone raced this
# and the game died with "Failed to load module '.../sdk64/steamclient.so'"), (b) a Steam
# process is alive, and (c) the client is ACTUALLY AUTHENTICATED this session
# (client_authenticated: steamwebhelper -steamid != 0). A running-but-logged-out client
# loads steamclient.so fine yet SteamAPI_Init() still returns False (steamid=0), stuck on
# SplashScreen forever. Condition (c) originally checked only loginusers.vdf existence, but
# that persists across a revoked token: on 2026-07-13 a steamcmd re-prime revoked the
# client's session token server-side, the file stayed, the gate passed on a dead login, and
# the game black-screened -- so (c) now requires a live non-zero steamid, not just the file.
# Each gate condition here was added after a boot that satisfied all prior ones yet still
# wasn't truly ready (bug four: process alive; bug eight: file present; this: really logged in).
STEAM_READY_TIMEOUT="${STEAM_READY_TIMEOUT:-600}"
STEAM_LOGIN_TIMEOUT="${STEAM_LOGIN_TIMEOUT:-1800}"
# When a human still has to VNC in and type a password, give them the longer budget.
WAIT_BUDGET="$STEAM_READY_TIMEOUT"
[[ "$STEAM_LOGIN_PENDING" -eq 1 ]] && WAIT_BUDGET="$STEAM_LOGIN_TIMEOUT"
STEAMCLIENT_SO="$STEAM_DIR/.steam/sdk64/steamclient.so"
STEAM_READY=0
SECONDS_WAITED=0
while [[ "$SECONDS_WAITED" -lt "$WAIT_BUDGET" ]]; do
    if [[ -e "$STEAMCLIENT_SO" ]] && pgrep -f "steamwebhelper|steam\.sh|/steam$" >/dev/null 2>&1 && client_authenticated; then
        STEAM_READY=1
        break
    fi
    # Silent auto-login path only: if the cached token was rejected server-side it will not
    # self-recover ("Do not reconnect") -- fail fast rather than burn the whole timeout.
    # Never on the human-login path, where a stale denial could pre-empt a login being typed.
    if [[ "$STEAM_LOGIN_PENDING" -eq 0 ]] && ! client_authenticated && client_auth_revoked; then
        log "Cached Steam client token was rejected server-side (Access Denied) -- aborting the readiness wait early; it will not self-recover."
        break
    fi
    if [[ "$STEAM_LOGIN_PENDING" -eq 1 && $((SECONDS_WAITED % 60)) -eq 0 && "$SECONDS_WAITED" -gt 0 ]]; then
        log "Still waiting for the one-time Steam login via VNC (localhost:5900)... ${SECONDS_WAITED}s/${WAIT_BUDGET}s"
    fi
    sleep 5
    SECONDS_WAITED=$((SECONDS_WAITED + 5))
done
if [[ "$STEAM_READY" -ne 1 ]]; then
    if [[ "$STEAM_LOGIN_PENDING" -eq 1 ]]; then
        fatal "No Steam login landed within ${WAIT_BUDGET}s. Connect a VNC viewer to localhost:5900 and log in as '$STEAM_ACCOUNT' (see the banner above), then restart the container -- or raise STEAM_LOGIN_TIMEOUT if you just need more time."
    fi
    if client_auth_revoked && ! client_authenticated; then
        fatal "Graphical Steam client auto-login was REJECTED server-side ('Access Denied' in $CONNECTION_LOG): the cached client session token was REVOKED (this is NOT a time-expiry -- the JWT can still be years from expiring). This typically happens after a steamcmd password re-prime, which rotates the account credential and invalidates the graphical client's SEPARATE session token. The client will not self-recover. Remediation: connect a VNC viewer to localhost:5900, log in to Steam as '$STEAM_ACCOUNT' (password + Steam Guard) with 'Remember me' ENABLED, then restart the container so a fresh game process runs SteamAPI_Init against the now-logged-in client."
    fi
    fatal "Steam client did not become ready within ${WAIT_BUDGET}s ($STEAMCLIENT_SO missing, Steam process gone, or the client never authenticated this session -- steamwebhelper still -steamid=0). If the cached token was revoked, log in again via VNC (localhost:5900) with 'Remember me' on; otherwise increase STEAM_READY_TIMEOUT if this is just a slow cold boot."
fi
log "Steam readiness confirmed after ${SECONDS_WAITED}s: steamclient.so present, client process alive, client authenticated (steamwebhelper -steamid != 0, or a CM 'OK' logon for a non-zero steamid in the connection log)."
# Logged in just now via the UI? Give the client a moment to finish post-login init
# (friends/IPC services settle) before the game tries SteamAPI_Init against it.
[[ "$STEAM_LOGIN_PENDING" -eq 1 ]] && { log "Fresh interactive login detected -- settling 15s before game launch."; sleep 15; }

# ---------- AppID identification for direct launch ----------
# The game is launched directly (run_bepinex.sh), not through the Steam client UI, and the
# steamcmd-managed install lives outside the client's own steamapps library -- so
# SteamAPI_Init cannot resolve which app is calling and fails even with a live, logged-in
# Steam client (found live 2026-07-12: IsSteamRunning()=True, steamclient.so loads OK, yet
# Init() returns False; the host setup never hits this because there the game IS installed
# in the client's own library). Standard Steamworks fix for direct launches: provide the
# AppID via the SteamAppId env var (inherited by the game process; cwd-independent) plus a
# steam_appid.txt next to the binary as a fallback for anyone launching it by hand.
export SteamAppId="$LIFTOFF_APP_ID"
export SteamGameId="$LIFTOFF_APP_ID"
echo "$LIFTOFF_APP_ID" > "$LIFTOFF_INSTALL_DIR/steam_appid.txt"
log "Set SteamAppId=$LIFTOFF_APP_ID and wrote steam_appid.txt for direct game launch."

# ---------- hand off to the orchestrator ----------
SHUFFLE_FLAG=""; [[ "${SHUFFLE,,}" == "true" ]] && SHUFFLE_FLAG="--shuffle"
PUBLIC_FLAG=""; [[ "${PUBLIC,,}" == "true" ]] && PUBLIC_FLAG="--public"
AUTOSTART_FLAG=""; [[ "${AUTO_START,,}" == "true" ]] && AUTOSTART_FLAG="--auto-start"
MAXPLAYERS_ARGS=()
[[ -n "$MAX_PLAYERS" ]] && MAXPLAYERS_ARGS=(--max-players "$MAX_PLAYERS")

log "Launching orchestrator: playlist=$PLAYLIST interval=${INTERVAL}s shuffle=$SHUFFLE public=$PUBLIC auto_start=$AUTO_START"
cd "$PROJECT_DIR"
exec python3 orchestrator/run_headless_lobby.py \
    --playlist "$PLAYLIST" \
    --interval "$INTERVAL" \
    --width "$WIDTH" \
    --height "$HEIGHT" \
    --lobby-name "$LOBBY_NAME" \
    $SHUFFLE_FLAG $PUBLIC_FLAG $AUTOSTART_FLAG "${MAXPLAYERS_ARGS[@]}"
