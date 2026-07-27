# Liftoff Auto Lobby

[![CI](https://github.com/barhoomi/liftoff-autolobby/actions/workflows/ci.yml/badge.svg)](https://github.com/barhoomi/liftoff-autolobby/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20tested%20·%20Windows%20untested-blue.svg)](#install-player)
[![Game](https://img.shields.io/badge/game-Liftoff%3A%20FPV%20Drone%20Racing-8A2BE2.svg)](https://store.steampowered.com/app/410340/)
[![Bug reports](https://img.shields.io/badge/bugs-GitHub%20Issues-orange.svg)](https://github.com/barhoomi/liftoff-autolobby/issues)

An unofficial [BepInEx](https://github.com/BepInEx/BepInEx) plugin for *Liftoff: FPV Drone
Racing* (Steam) that runs an auto-rotating multiplayer lobby: pick a list of tracks, and the
plugin cycles through them on a timer in a room you host, with chat commands to control
everything.

The repo also contains a standalone procedural track generator (pure Python, no game
required) — see [`generator/`](generator/).

> [!WARNING]
> **Tested on Linux only so far.** Windows support is implemented but has not been
> verified on real hardware yet. If you run it on Windows, expect rough edges — and please
> [open an issue](https://github.com/barhoomi/liftoff-autolobby/issues) telling us how it went,
> even if it worked fine.

> [!IMPORTANT]
> **This project is not official, is not affiliated with, and is not supported by LuGus
> Studios.** It drives the game's own UI from inside the process — the same technique other
> Liftoff BepInEx mods use. No anti-cheat is known to exist in Liftoff, and LuGus has made
> no statement about code mods either way. Use at your own risk, on an account you're
> comfortable with.

## Found a bug?

**[Open a GitHub issue](https://github.com/barhoomi/liftoff-autolobby/issues).** Include:

1. The output of `/version` (typed in room chat).
2. Whether you're on the player or server package, and your OS.
3. The relevant excerpt from the **game's** `Player.log` (see [Logs](#logs) — not the
   BepInEx log).
4. What you did, what you expected, and what happened.

## Two ways to run it

| | **Player package** | **Server package** |
|---|---|---|
| For | Auto-rotating tracks in a room you host, on your own account | A persistent, unattended 24/7 lobby |
| Platform | Linux (Windows untested) | Linux (Docker) |
| Mode | `client` — plugin sits idle until you type `/start` in a room you host; you are automatically the admin | `server` — a Python orchestrator handles Steam login, game launch, recovery, and playlists on a dedicated Steam account |

Until a tagged release exists there are no downloadable zips yet — build from source
(below). Release zips will be `liftoff-autolobby-player-vX.Y.Z.zip` and
`liftoff-autolobby-server-vX.Y.Z.zip`.

## Install (player)

1. Install [BepInEx 5.x](https://github.com/BepInEx/BepInEx/releases) — the **Mono** build
   (Liftoff is a Mono Unity game) — into your Liftoff install folder, and launch the game
   once so BepInEx generates its folder tree. On Linux that means extracting the Unix zip
   into e.g. `~/.steam/steam/steamapps/common/Liftoff`, `chmod +x run_bepinex.sh`, and
   setting the Steam launch option `/path/to/Liftoff/run_bepinex.sh %command%` (or running
   that script directly).
2. Extract the player zip's `BepInEx/` folder over the game's `BepInEx/` folder. This puts
   `LiftoffAutoLobby.dll` and a starter `tracks_to_rotate.txt` **directly in
   `BepInEx/plugins/`** (flat — the plugin reads all its config/state files from that
   folder, so don't move them into a subfolder), plus a config file in `BepInEx/config/`
   that sets the plugin to client mode.
3. Launch the game, join or host a multiplayer room, and type `/version` in chat to
   confirm it's alive.

If you built from source instead of using a zip: copy the DLL into `BepInEx/plugins/` and
create `BepInEx/config/com.barhoomi.liftoff.autolobby.cfg` containing:

```ini
[General]
Role = client
```

> [!CAUTION]
> Without that config the plugin defaults to `server` mode, which automates menus and
> creates rooms by itself — you don't want that on a personal account.

## Build a playlist

On every launch the plugin writes `BepInEx/plugins/track_mode_availability.json` — every
track × environment × game-mode combination your install actually owns (base game plus
subscribed Workshop tracks). Copy names from there (or browse in-game with
`/tracks [keyword]`) into `BepInEx/plugins/tracks_to_rotate.txt`, one per line:

```
# Format: TrackName, Environment, GameMode   (lines starting with # are ignored)
Sunset Sprint, The Drawing Board, Classic Race
Canyon Run, Kastle Klash, Infinite Race
```

Line order is the rotation order (unless shuffle is on). If the mode is omitted the
plugin falls back to `Classic Race`. Editing this file by hand is the whole playlist
story for now — there's no in-game playlist authoring yet.

## Use it

Host a multiplayer room, then type `/start` in the room chat. The plugin loads your
playlist, announces the first track, and starts the rotation timer. As the host, you're
automatically the admin. `/pause` / `/resume` freeze the timer; `/stop` goes back to
idle.

> [!TIP]
> `/help` (in chat) always shows the current command list *you* are allowed to run — it's
> the live source of truth if this table ever drifts.

| Command | Who | What |
|---|---|---|
| `/help [page]` | Anyone | List available commands. |
| `/version` | Anyone | Show the running plugin version. |
| `/info` | Anyone | Current playlist, timer, track, and room info. |
| `/tracks [keyword] [page]` | Anyone | Search/browse available tracks. |
| `/history` | Anyone | Recently played tracks. |
| `/players` | Anyone | List players in the room. |
| `/start` `/stop` `/pause` `/resume` | Admin | Rotation lifecycle. |
| `/skip` | Admin (anyone if democracy is on) | Next track. |
| `/prev` | Admin | Previous track. |
| `/track <number>` | Admin | Load a track by its `/tracks` index. |
| `/extend <seconds>` | Admin | Extend the current track's timer. |
| `/interval <seconds>` | Admin | Set the rotation interval (min 30s). |
| `/shuffle on\|off` | Admin | Toggle shuffle. |
| `/democracy on\|off` | Admin | Toggle public `/skip` voting. |
| `/mode infinite\|circuit\|dropout\|survival\|auto` | Admin | Override the game mode. |
| `/maxplayers <n>` | Admin | Room player limit (min 2). |
| `/private [name]` / `/public` | Admin | Room visibility. |
| `/promote <username>` | Admin | Hand room host to another player. |
| `/kick <player_name>` | Admin | Kick a player. |
| `/playlist [name]` | Admin | Show/switch playlist (server mode). |
| `/maintenance [minutes\|cancel]` | Admin | Schedule a shutdown (server mode). |
| `/reloadtheme` | Admin | Reload chat colors from `chat_theme.json`. |

## Logs

The plugin's runtime output (commands, rotation, errors — what you want for bug reports)
goes to the **game's** log, not BepInEx's:

- Linux: `~/.config/unity3d/LuGus Studios/Liftoff/Player.log` — look for
  `[AutoLobbyPlugin]` lines.
- Windows (unverified): `%USERPROFILE%\AppData\LocalLow\LuGus Studios\Liftoff\Player.log`.

`BepInEx/LogOutput.log` only tells you whether the DLL loaded at all.

## Upgrading / uninstalling

Close the game, replace `LiftoffAutoLobby.dll`, relaunch — your `tracks_to_rotate.txt`
and other config files are untouched. To uninstall, delete the DLL from
`BepInEx/plugins/`; the plugin writes nothing outside that folder.

## Server operators

The server package runs the same DLL in `server` mode: a Python orchestrator manages
Steam login, launch/recovery, and playlists, and writes the plugin's config as text files
in `BepInEx/plugins/` instead of chat commands. Docker (`Dockerfile`,
`docker-compose.yml`, `infra/docker-entrypoint.sh`) is the supported way to run it —
starter config lives in `config/`, and the plugin⇄orchestrator file protocol is
documented in `orchestrator/run_headless_lobby.py`. It needs its own dedicated Steam
account (one interactive login to prime the session, then it's unattended).

## Contributing / building from source

- Plugin: `dotnet build plugin/ -c Debug` (game reference DLLs required — copy them from
  your own install per `plugin/libs/README.md`, or pass
  `-p:LiftoffPath=/path/to/your/Liftoff` to build straight against it).
- Python: `bash scripts/run_tests.sh` runs the pytest suites (generator, orchestrator,
  trackcheck) plus the playlist lint.
- Repo layout: `plugin/` (C# BepInEx plugin), `orchestrator/` (launcher/watchdog +
  scenario tests), `generator/` (procedural track generator), `trackcheck/`
  (track/playlist validation), `config/` (runtime config), `scripts/` (dev + ops entry
  points), `infra/` (setup, Docker entrypoint, release packaging).

Bug reports and PRs welcome — [GitHub issues](https://github.com/barhoomi/liftoff-autolobby/issues)
is the place.

## License

[MIT](LICENSE). The game's own assemblies (LuGus/Unity/Photon) are **not** part of this
repository or its releases — the build references them from your own Liftoff install.
