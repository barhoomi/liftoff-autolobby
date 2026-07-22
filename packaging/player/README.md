# LiftoffAutoLobby -- Player Package

An unofficial BepInEx mod for *Liftoff: FPV Drone Racing* that auto-rotates tracks in a
multiplayer room you host, plus a handful of chat commands. Not affiliated with or
supported by LuGus Studios.

This is a starting install guide; see the project repository's README for the full,
maintained version (`https://github.com/barhoomi/procedural-fpv`).

## Install

1. Install BepInEx 5.4.21 (Mono) into your Liftoff install if you haven't already:
   https://github.com/BepInEx/BepInEx/releases -- pick the `BepInEx_x64_5.4.21.0` (or
   later 5.x) build matching your OS, extract it into the Liftoff game folder, and run the
   game once so BepInEx generates its folders.
2. Extract this zip's `BepInEx/` folder into your Liftoff install root, merging with the
   existing `BepInEx/` folder. This drops `LiftoffAutoLobby.dll` and an example
   `tracks_to_rotate.txt` into `BepInEx/plugins/LiftoffAutoLobby/`.
3. Launch the game, host a multiplayer room, and check the chat log or
   `BepInEx/LogOutput.log` for `[AutoLobbyPlugin]` lines confirming it loaded. Run
   `/version` in chat to confirm.

## Set up your playlist

`tracks_to_rotate.txt` ships as an example/starter -- edit it to list tracks you actually
own before starting rotation. See the comments inside that file for the exact format and
where to find your own track names (the plugin writes a `track_mode_availability.json`
listing everything your install has).

## Commands

Run `/help` in the room's chat for the full, current list. Everyone in the room can run
public commands (`/info`, `/version`, `/help`, ...); the room host needs to be recognized
as an admin (see the main repo README for how admin IDs are configured) to run
room-management commands (`/start`, `/skip`, `/interval`, ...).

## Logs

- Plugin: `BepInEx/LogOutput.log`
- Game (richer runtime detail -- chat/commands/rotation): your Liftoff `Player.log`
  (platform-dependent path; see BepInEx's own docs for where Unity puts it on your OS).

## Uninstall

Delete `BepInEx/plugins/LiftoffAutoLobby/`. If you no longer want BepInEx at all, remove
the rest of the `BepInEx/` folder and `winhttp.dll`/`doorstop_config.ini` (Windows) or the
equivalent loader files (Linux) that the BepInEx installer added.

## Not official

This is a third-party fan project. It automates the game's own UI and chat; it does not
modify game files or read/write anything outside your Liftoff install and BepInEx's own
folders. Use at your own risk, same as any other mod.
