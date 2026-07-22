# LiftoffAutoLobby -- Server Package

The dedicated-bot half of the plugin: the same `LiftoffAutoLobby.dll` as the player
package, run in `role = server` mode, plus the Python orchestrator that launches and
supervises it, the procedural track generator, and Docker packaging.

This is a starting operations guide; see the project repository's README and
`docs/features/` for the maintained version and full architecture notes
(`https://github.com/barhoomi/procedural-fpv`).

## Contents

```
BepInEx/plugins/LiftoffAutoLobby/LiftoffAutoLobby.dll   the plugin, role=server
orchestrator/                                             launcher, watchdog, track gather
generator/                                                procedural track generator
trackcheck/                                                track/playlist validation library
infra/                                                     setup + Docker entrypoint scripts
Dockerfile, docker-compose.yml                             containerized bot
lobby_config.json, playlists.json                          starter config -- edit before running
```

## Run it

The supported path is Docker: `docker compose up`. See `Dockerfile` and
`infra/docker-entrypoint.sh` for what it does (Xvfb + Steam + steamcmd + BepInEx-patched
Liftoff + the orchestrator, all inside the container) and `docker-compose.yml` for the
volumes it expects (`/steam`, `/logs`, `/config`).

A one-time interactive Steam login against the `/steam` volume is required before the bot
can run unattended -- see the main repo's `docs/` for the exact steps; this package does
not include a dedicated Steam account for you.

## Configure

Edit `lobby_config.json` (Liftoff path inside the container, display, lobby name) and
`playlists.json` (named track playlists) before starting. The plugin ⇄ orchestrator
protocol is plain text files under BepInEx's `plugins/LiftoffAutoLobby/` directory
(`tracks_to_rotate.txt`, `rotation_state.txt`, `admin_ids.txt`, ...) -- the orchestrator
writes them, the plugin polls them.

## Logs

Mount `/logs` to a host directory to keep plugin and orchestrator logs across container
restarts.

## Not official

This is a third-party fan project, not affiliated with or supported by LuGus Studios.
