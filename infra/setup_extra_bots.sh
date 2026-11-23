#!/bin/bash
set -euo pipefail

# setup_extra_bots.sh - Create additional bot users (fpv_bot2, fpv_bot3), let the dev user
# drive them passwordlessly, and pre-seed each one's Steam library with a copy of the dev
# user's installed Liftoff so Steam sees it as already installed (no download).
#
# Usage: sudo bash infra/setup_extra_bots.sh [N ...]     (default: 2 3)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo." >&2
    exit 1
fi

DEV_USER="${SUDO_USER:?run via sudo from your normal account}"
DEV_HOME="$(getent passwd "$DEV_USER" | cut -d: -f6)"
SRC_APPS="$DEV_HOME/.steam/debian-installation/steamapps"
SRC_GAME_DIR="$SRC_APPS/common/Liftoff"
SRC_MANIFEST="$SRC_APPS/appmanifest_410340.acf"
BOT_NUMS=("$@")
[ ${#BOT_NUMS[@]} -gt 0 ] || BOT_NUMS=(2 3)

[ -d "$SRC_GAME_DIR" ] || { echo "ERROR: $SRC_GAME_DIR not found" >&2; exit 1; }
[ -f "$SRC_MANIFEST" ] || { echo "ERROR: $SRC_MANIFEST not found" >&2; exit 1; }

SUDOERS_USERS=()
for n in "${BOT_NUMS[@]}"; do
    user="fpv_bot$n"
    echo "=== $user ==="

    # 1. User account (locked password: driven via sudo -u, not GDM login)
    if id "$user" &>/dev/null; then
        echo "User exists, skipping creation."
    else
        useradd -m -s /bin/bash -c "Bar's Bot $n" -G video,render "$user"
        echo "Created $user (uid $(id -u "$user"))."
    fi
    home="$(getent passwd "$user" | cut -d: -f6)"
    SUDOERS_USERS+=("$user")

    # 2. Persistent /run/user/<uid> + user manager (survives reboot, unlike a manual mkdir)
    loginctl enable-linger "$user"

    # 3. Seed Steam library with Liftoff. Steam's first-run bootstrap extracts into
    #    ~/.steam/debian-installation and leaves an existing steamapps/ alone.
    dest_apps="$home/.steam/debian-installation/steamapps"
    install -d -o "$user" -g "$user" "$home/.steam" "$home/.steam/debian-installation" "$dest_apps" "$dest_apps/common"
    echo "Copying Liftoff (~26 GB)..."
    rsync -a --info=progress2 --chown="$user:$user" \
        --exclude='Liftoff_Data/Credentials/' \
        --exclude='mono_crash.*' \
        --exclude='*log.txt' --exclude='game_stderr.txt' \
        --exclude='BepInEx/LogOutput.log' \
        "$SRC_GAME_DIR/" "$dest_apps/common/Liftoff/"
    install -o "$user" -g "$user" -m 644 "$SRC_MANIFEST" "$dest_apps/appmanifest_410340.acf"
    chmod +x "$dest_apps/common/Liftoff/"*.sh "$dest_apps/common/Liftoff/Liftoff.x86_64"
done

# 4. Let the dev user act as the bots without a password (same as fpv_bot has today)
SUDOERS_FILE=/etc/sudoers.d/fpv-extra-bots
existing="$(sed -n 's/^.*=(\(.*\)) NOPASSWD: ALL$/\1/p' "$SUDOERS_FILE" 2>/dev/null | tr ',' '\n')"
all_users="$(printf '%s\n' $existing "${SUDOERS_USERS[@]}" | sort -u | paste -sd,)"
tmp="$(mktemp)"
echo "$DEV_USER ALL=($all_users) NOPASSWD: ALL" > "$tmp"
if visudo -cf "$tmp" >/dev/null; then
    install -m 440 "$tmp" "$SUDOERS_FILE"
    echo "Sudoers: $DEV_USER -> ($all_users) passwordless."
else
    echo "ERROR: generated sudoers rule failed validation; not installed." >&2
fi
rm -f "$tmp"

echo ""
echo "=== Done ==="
echo "Launch Steam for a bot on your screen:  steam-as fpv_bot2"
echo "First launch: accept Steam's terms, log into that bot's Steam account; Liftoff shows as installed."
