#!/usr/bin/env bash
# Interactive walkthrough for the first end-to-end test of the Docker container
# (docs/features/doing/docker-container.md). Run this from a real terminal — the
# credential-priming step needs to read your Steam password / Steam Guard code
# interactively, which will not work piped through anything else.
#
# Usage: bash infra/docker-test-walkthrough.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

step() {
    echo
    echo "================================================================"
    echo "  $1"
    echo "================================================================"
}

pause() {
    read -r -p ">> Press Enter to continue (or Ctrl-C to stop here)... "
}

confirm() {
    local prompt="$1"
    local reply
    read -r -p "$prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

step "0. Preflight checks"
if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not on PATH. Install Docker first, then re-run this script."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "docker compose (v2 plugin) not found. Install it, then re-run this script."
    exit 1
fi
echo "docker + docker compose found."
echo
echo "IMPORTANT: this test uses the bot's real Steam account (\"Bar's Bot\")."
echo "Do not run this at the same time as the live host bot (bare-metal launch) —"
echo "the two Steam sessions will kick each other."
if pgrep -af 'run_headless_lobby.py|Liftoff\.x86_64$' >/dev/null 2>&1; then
    echo
    echo "WARNING: it looks like the host bot (or the game) is currently running."
    echo "Stop it first (kill_bot.sh) before continuing."
    pause
fi

step "1. Configure .env"
if [[ -f .env ]]; then
    echo ".env already exists — leaving it as-is."
else
    cp .env.example .env
    echo "Created .env from .env.example."
fi
echo
echo "Open .env now and set STEAM_ACCOUNT to the bot's Steam login name."
echo "(All other values have sane defaults — leave them unless you have a reason to change them.)"
pause
echo "Current STEAM_ACCOUNT setting:"
grep -E '^STEAM_ACCOUNT=' .env || echo "  (not set — the container will fail fast until you set it)"
if grep -qE '^STEAM_ACCOUNT=your_bot_steam_account$' .env; then
    echo
    echo "STEAM_ACCOUNT still has the placeholder value. Edit .env before continuing."
    exit 1
fi

step "2. Build the image"
echo "This builds a ~1.4GB image (Ubuntu + Steam + steamcmd + .NET + Python). A few minutes."
if confirm "Run 'docker compose build' now?"; then
    docker compose build
else
    echo "Skipped — run 'docker compose build' yourself before continuing."
fi

step "3. Prime Steam credentials (one-time, interactive)"
echo "This is the one step that needs YOU — it will prompt for the bot account's"
echo "Steam password and a Steam Guard code. The login gets cached into the"
echo "'steam_data' named volume, so you only need to do this once (until the"
echo "cached token expires or is revoked)."
echo
echo "About to run:"
echo "  docker compose run --rm -it -u botuser -e HOME=/steam --entrypoint /usr/games/steamcmd bot \\"
echo "      +force_install_dir /steam/Liftoff +login \$STEAM_ACCOUNT +quit"
pause
# Don't `source .env` — it's parsed by Docker Compose's env-file rules (unquoted
# spaces allowed, e.g. LOBBY_NAME=Procedural Loop Room), which is NOT valid bash
# syntax. Pull just the one value we need instead.
STEAM_ACCOUNT="$(grep -E '^STEAM_ACCOUNT=' .env | head -n1 | cut -d= -f2-)"
if [[ -z "$STEAM_ACCOUNT" ]]; then
    echo "Could not read STEAM_ACCOUNT from .env — check the file."
    exit 1
fi
docker compose run --rm -it -u botuser -e HOME=/steam --entrypoint /usr/games/steamcmd bot \
    +force_install_dir /steam/Liftoff +login "$STEAM_ACCOUNT" +quit
echo
if confirm "Did steamcmd report a successful login (not an auth failure)?"; then
    echo "Good — credentials are cached."
else
    echo "Stopping here. Re-run this script (it will skip straight back to this step"
    echo "since .env and the image are already set up) once you're ready to retry login."
    exit 1
fi

step "4. Bring the bot up"
echo "This starts steamcmd's game install/update, BepInEx install, the plugin build,"
echo "the graphical Steam client under Xvfb, and finally the orchestrator — in that order."
echo "First run downloads the full game, so this can take a while depending on bandwidth."
echo
echo "ONE MORE one-time manual step happens during this stage: the graphical Steam"
echo "client keeps its OWN login (steamcmd's cached token can't log it in). On the"
echo "first boot of a fresh steam_data volume the entrypoint pauses, prints a banner,"
echo "and waits for you to:"
echo "  - connect a VNC viewer to localhost:5900  (e.g. 'vncviewer localhost:5900',"
echo "    or Remmina/TigerVNC — anything that speaks VNC)"
echo "  - log in to Steam in that window (password + Steam Guard, keep 'Remember me')"
echo "The container then continues on its own, and future starts skip this entirely."
echo
echo "Watch the logs for these milestones, in order:"
echo "  1. steamcmd +app_update 410340 completing (no timeout/auth-failure message)"
echo "  2. BepInEx install succeeding"
echo "  3. dotnet build succeeding against the real install"
echo "  4. Steam client ready: logged in (loginusers.vdf) + steamclient.so present"
echo "     (first boot: do the VNC login described above when the banner appears)"
echo "  5. Game launching with SteamAPI_Init succeeding (NOT a steamid=0 black-screen)"
echo "  6. The orchestrator/plugin reaching a working lobby state"
echo
if confirm "Run 'docker compose up' now in the foreground (Ctrl-C to stop)?"; then
    docker compose up
else
    echo "Skipped. When you're ready:"
    echo "  docker compose up          # foreground, logs inline"
    echo "  docker compose up -d && docker compose logs -f   # detached + follow"
fi

step "Done"
echo "When you're finished testing:"
echo "  docker compose down"
echo
echo "Record what you observed (pass/fail per milestone above, and any log excerpts)"
echo "in docs/features/doing/docker-container.md's 'What still needs human/live"
echo "verification' checklist before this feature can move to done/."
