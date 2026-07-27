# Liftoff Auto Lobby — Player Package

An unofficial BepInEx mod for *Liftoff: FPV Drone Racing* that auto-rotates tracks in a
multiplayer room you host, controlled by chat commands. Not affiliated with or supported
by LuGus Studios.

> **Tested on Linux only so far.** Windows is expected to work but has not been verified
> on real hardware yet — if you try it, please report how it goes either way:
> https://github.com/barhoomi/procedural-fpv/issues

Full, maintained documentation: https://github.com/barhoomi/procedural-fpv

## Install

1. Install [BepInEx 5.x](https://github.com/BepInEx/BepInEx/releases) (the **Mono** build)
   into your Liftoff install folder, and run the game once so BepInEx generates its
   folders.
2. Extract this zip's `BepInEx/` folder into the Liftoff install root, merging with the
   existing `BepInEx/` folder. This puts `LiftoffAutoLobby.dll` and a starter
   `tracks_to_rotate.txt` directly in `BepInEx/plugins/` (they must sit there flat, not
   in a subfolder), and a config file in `BepInEx/config/` that keeps the plugin in
   **client mode** — it never automates menus or touches anything until you ask it to.
3. Launch the game and type `/version` in any multiplayer room's chat to confirm it
   loaded.

## Build a playlist and start

1. Launch the game once — the plugin writes `BepInEx/plugins/track_mode_availability.json`
   listing every track/environment/mode combination your install owns (base game +
   subscribed Workshop tracks). You can also browse in-game with `/tracks [keyword]`.
2. Edit `BepInEx/plugins/tracks_to_rotate.txt` — one `Track, Environment, Mode` line per
   track, names copied verbatim from that file. Comments start with `#`.
3. Host a multiplayer room and type `/start`. As the room's host you are automatically
   the admin — no extra setup. `/pause`, `/resume`, and `/stop` control the rotation;
   `/help` lists everything you're allowed to run.

## Logs (for bug reports)

The plugin's runtime output (commands, rotation, errors) goes to the **game's** log, not
BepInEx's:

- Linux: `~/.config/unity3d/LuGus Studios/Liftoff/Player.log` — look for
  `[AutoLobbyPlugin]` lines.
- `BepInEx/LogOutput.log` only confirms the plugin loaded.

Report bugs at https://github.com/barhoomi/procedural-fpv/issues with the `/version`
output and the relevant `Player.log` excerpt.
