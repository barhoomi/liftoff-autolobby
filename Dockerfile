# Dockerfile — procedural-fpv bot, containerized.
#
# Reproduces the host flow (see AGENTS.md / run_bot.sh / infra/setup_bot.sh) inside an
# isolated image: Xvfb virtual display + full graphical Steam client (for the Steamworks
# runtime IPC the game binary needs at launch -- NOT just steamcmd; see the "Spec conflict"
# section of docs/features/doing/docker-container.md for why both are installed) +
# steamcmd (content install/update) + BepInEx-patched Liftoff + the Python orchestrator.
#
# The actual game install, BepInEx deployment, and plugin compilation happen at CONTAINER
# RUNTIME (infra/docker-entrypoint.sh), not at `docker build` time -- the paid game can only
# be fetched with a human-primed Steam login against the persistent /steam volume, which
# doesn't exist yet during image build.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# --- Steam needs i386 packages (the client + steamcmd are still 32-bit on Linux) ---
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl wget gnupg lsb-release && \
    add-apt-repository -y multiverse && \
    apt-get update

# Pre-accept the Steam EULA so `apt-get install steamcmd steam-installer` never blocks on an
# interactive debconf prompt (there is no human at the console during `docker build`).
RUN echo steam steam/question select "I AGREE" | debconf-set-selections && \
    echo steam steam/license note '' | debconf-set-selections

RUN apt-get update && apt-get install -y --no-install-recommends \
        steamcmd \
        steam-installer \
        xvfb \
        x11-utils \
        x11vnc \
        dbus-x11 \
        libgl1 libglu1-mesa \
        lib32gcc-s1 lib32stdc++6 \
        python3 python3-pip python3-venv \
        unzip git sudo procps tini \
    && rm -rf /var/lib/apt/lists/*

# --- .NET SDK (compiles the BepInEx plugin at container-startup, once the game's Managed
# DLLs exist on the mounted /steam volume -- see docker-entrypoint.sh) ---
RUN wget -q https://packages.microsoft.com/config/ubuntu/22.04/packages-microsoft-prod.deb -O /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends dotnet-sdk-8.0 \
    && rm -rf /var/lib/apt/lists/*

# --- Unprivileged bot user. Steam refuses to run as root, and running the whole stack as
# root inside the container is unnecessary. /steam doubles as this user's $HOME so the
# Debian Steam client's own `~/.steam/...` / `~/.local/share/Steam/...` paths land on the
# persistent volume without extra path plumbing. ---
RUN useradd -m -u 1000 -d /steam -s /bin/bash botuser \
    && mkdir -p /steam /logs /config /run/user/1000 \
    && chown -R botuser:botuser /steam /logs /config /run/user/1000 \
    && chmod 700 /run/user/1000

WORKDIR /app
COPY --chown=botuser:botuser . /app

# WORKDIR creates /app as root; COPY --chown fixes the copied *contents* but not the
# directory entry itself, which botuser then can't write into (e.g. the entrypoint's
# lobby_config.json/playlists.json symlink-into-/config step) without this.
RUN chown botuser:botuser /app \
    && chmod +x /app/infra/docker-entrypoint.sh /app/infra/install_bepinex.sh

VOLUME ["/steam", "/logs", "/config"]

# Placeholder for the future dashboard mentioned in the feature spec; nothing listens here
# yet -- see docs/features/todo (or backlog) for that work.
EXPOSE 8080

# x11vnc on the Xvfb display -- required ONCE per /steam volume for the graphical Steam
# client's interactive login (steamcmd's cached token is machine-scoped and cannot log the
# graphical client in; see docker-entrypoint.sh), and useful any time for watching the
# game render. docker-compose.yml binds it to 127.0.0.1 on the host only.
EXPOSE 5900

ENV STEAM_DIR=/steam \
    LOG_DIR=/logs \
    CONFIG_DIR=/config \
    FPV_LOG_DIR=/logs \
    DISPLAY=:99 \
    XDG_RUNTIME_DIR=/run/user/1000 \
    PROJECT_DIR=/app

# tini as PID 1: reaps zombie processes (Xvfb, the Steam client, and the game process are
# all forked as siblings by docker-entrypoint.sh rather than exec'd) and forwards signals
# so `docker stop` doesn't have to fall back to SIGKILL every time.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/infra/docker-entrypoint.sh"]
