# Liftoff Auto Lobby

An unofficial [BepInEx](https://github.com/BepInEx/BepInEx) plugin for *Liftoff: FPV Drone
Racing* (Steam) that runs an auto-rotating multiplayer lobby: pick a list of tracks, and the
plugin cycles through them on a timer, in a room you host, with chat commands to control it.

This repository also contains a standalone procedural track generator (pure Python, no game
required) — see [`generator/`](generator/) if that's what you're looking for.

> **This project is not official, is not affiliated with, and is not supported by LuGus
> Studios.** It works by driving the game's own UI and reading its own data structures from
> inside the process — the same technique other Liftoff BepInEx mods use. No anti-cheat is
> known to exist in Liftoff, and no statement from LuGus Studios about code mods (for or
> against) was found either way. That is not a green light — it is genuinely unknown. Use at
> your own risk, on an account you're comfortable with.

## Which package do I want?

| | **Player package** | **Server package** |
|---|---|---|
| For | Someone who wants auto-rotating tracks in a room they host on their own Steam account | Someone running a persistent, unattended lobby (what this project's own dev lobby runs on) |
| Platform | Windows or Linux | Linux (Docker) |
| Setup | Install BepInEx + the DLL, hand-edit a playlist file, `/start` | Orchestrator + Docker, own dedicated Steam account |
| Config | Chat commands; host is automatically the admin | Text-file protocol written by the Python orchestrator, `admin_ids.txt` for admins |

`TBD(build-release-pipeline)`: the exact download names and zip contents are not final — this
section will be updated with the release-page links and exact filenames once R1 ships. The
planned layout (from `docs/features/todo/build-release-pipeline.md`) is:

```
liftoff-autolobby-player-vX.Y.Z.zip
  BepInEx/plugins/LiftoffAutoLobby.dll
  tracks_to_rotate.txt      (starter file with a commented-out example)
  README.md

liftoff-autolobby-server-vX.Y.Z.zip
  BepInEx/plugins/LiftoffAutoLobby.dll
  orchestrator/ generator/ trackcheck/ infra/
  Dockerfile docker-compose.yml lobby_config.json playlists.json
  README.md
```

If you're reading this before a tagged release exists, there is no downloadable zip yet — build
from source per [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md).

## Requirements

- *Liftoff: FPV Drone Racing* on Steam.
- [BepInEx 5.x](https://github.com/BepInEx/BepInEx/releases) — the **Mono**, not IL2CPP, build
  (Liftoff is a Mono Unity game).
- Windows or Linux. `TBD(windows-compatibility)`: Windows support is implemented but not yet
  verified on a real Windows machine — see the Known Issues note below before relying on it.

## Install

### 1. Install BepInEx first

BepInEx has to be in place and the game launched at least once through it *before* you add this
plugin — that's what generates the `BepInEx/plugins/` folder the plugin's files live in.

**Windows** — `TBD(windows-compatibility)`: exact verified steps are pending a live pass on a
Windows machine (tracked in `docs/features/todo/windows-compatibility.md`). Expected steps,
based on BepInEx's own documentation and how another Liftoff BepInEx mod is installed:

1. Download the BepInEx 5.x **x64** (or x86, matching the game) zip for Windows.
2. Extract it into the Liftoff install folder (the folder containing `Liftoff.exe`), so
   `winhttp.dll` sits next to the game executable.
3. Launch the game once through Steam and quit again, so BepInEx generates its folder tree
   (`BepInEx/plugins/`, `BepInEx/config/`, etc.).
4. If you're running the Windows build through **Proton** on Linux/Steam Deck, BepInEx's DLL
   hijack needs a launch option: right-click Liftoff in Steam → Properties → Launch Options →
   `WINEDLLOVERRIDES="winhttp.dll=n,b" %command%`.
5. `TBD(windows-compatibility)`: another Liftoff BepInEx mod's README says it won't load
   reliably unless `BepInEx.cfg` has `EnableAssemblyCache = false` and
   `HideManagerGameObject = true`. Whether this plugin needs that too is still being checked —
   if you hit a "plugin never activates" symptom, try that first and report back.

**Linux (native)**:

1. Download the BepInEx 5.x Unix zip.
2. Extract it into your Liftoff install directory, e.g.
   `~/.steam/steam/steamapps/common/Liftoff` (path varies by Steam library location —
   `infra/install_bepinex.sh` in this repo automates this step for the project's own dev/server
   setup and is a working reference for the commands involved).
3. `chmod +x run_bepinex.sh` in that folder.
4. Launch the game once via `./run_bepinex.sh`, or set it as a Steam launch option:
   `/path/to/Liftoff/run_bepinex.sh %command%`.

### 2. Add the plugin DLL

Drop `LiftoffAutoLobby.dll` directly into `BepInEx/plugins/` (the same folder BepInEx just
created — **not** a subfolder). This matters: the plugin resolves its own data directory as
`<gameRoot>/BepInEx/plugins` (flat — `plugin/Plugin.cs:136`), and every config/state file it
reads or writes (`tracks_to_rotate.txt`, `admin_ids.txt`, `rotation_state.txt`,
`track_mode_availability.json`, and the rest) lives there too. A DLL sitting in a subfolder
like `BepInEx/plugins/LiftoffAutoLobby/` would still load, but its config files would not be
found there. `TBD(build-release-pipeline)`: confirm the shipped zip matches this before
release — see the conflict note in `docs/features/doing/player-onboarding-ux.md`.

Launch the game. The plugin is now active but idle — it changes nothing in your game until you
tell it to.

## First run: finding track names and building a playlist

The plugin enumerates every environment × game-mode × track combination your install actually
owns (base-game tracks plus anything you've subscribed to on Steam Workshop) once per launch,
and writes it to `BepInEx/plugins/track_mode_availability.json`. That file is your menu of
valid options — names in it are already in the exact form the playlist file expects, so
building a playlist is copy-paste with no transcription errors.

You can also browse in-game without alt-tabbing: type `/tracks` (optionally
`/tracks <keyword>`) in a room's chat to page through matches.

Create (or edit the starter) `BepInEx/plugins/tracks_to_rotate.txt`. One line per track:

```
# Lines starting with # are comments/ignored. Format: TrackName, Environment, GameMode
# Copy exact names from track_mode_availability.json or in-game /tracks.
Sunset Sprint, The Drawing Board, Classic Race
Canyon Run, Kastle Klash, Infinite Race
```

- **Track name** and **environment** — copy verbatim from `track_mode_availability.json` or
  `/tracks`.
- **Game mode** — one of the modes that track/environment combination actually supports (also
  visible in `track_mode_availability.json`'s nesting). If you leave it off, the plugin falls
  back to `Classic Race`.
- Line order is the rotation order (or the shuffle deal, if shuffle is on).

This hand-edited file *is* v1's whole playlist story — there is no in-game playlist authoring
yet (planned for a later release; see `docs/features/backlog/ingame-playlist-authoring.md`).

## Starting rotation

`TBD(plugin-mode-split, client-lifecycle-commands)`: this is the target v1 flow and is **not
live yet** — it depends on two features still in `docs/features/todo/`:

- **`plugin-mode-split`** (R3) — makes the plugin recognize a player-hosted room and treat the
  host as the implicit admin (no `admin_ids.txt` to hand-edit).
- **`client-lifecycle-commands`** (R4) — adds `/start`, `/stop`, `/pause`, `/resume` so the
  player controls when rotation begins, rather than it auto-starting the way the server build
  does.

Once both land, the flow is:

1. Host a multiplayer room yourself, in-game, as normal.
2. Type `/start` in that room's chat. The plugin loads your playlist, announces the first
   track, and starts the rotation timer.
3. `/pause` / `/resume` freeze and unfreeze the timer without losing the current track.
   `/stop` returns to idle — the current track stays loaded, nothing further changes until you
   `/start` again.

Until those land, using any command below requires the classic server-mode setup (an
`admin_ids.txt` containing your Steam ID) — there is no player build yet.

## Chat command reference

All commands are typed into the in-game room chat. `/help` lists whatever commands *you*
personally are allowed to run, paginated — it's the live source of truth if this table and the
running plugin ever disagree.

Enumerated from `plugin/Commands/*.cs` — today's command set (server-mode oriented; see the
note below):

| Command | Who can run it | What it does |
|---|---|---|
| `/help [page]` | Anyone | List available commands, paginated. |
| `/tracks [keyword] [page]` | Anyone | List tracks matching a keyword, or all tracks, paginated. |
| `/track <number>` | Admin | Load a track by the index shown in the last `/tracks` search. |
| `/info` | Anyone | Show current playlist, rotation timer, current track, and room info. |
| `/history` | Anyone | Show the last few tracks played. |
| `/players` | Anyone | List active players in the lobby and their Photon IDs. |
| `/skip` | Admin, or anyone if democracy mode is on (majority vote) | Skip to the next track. |
| `/prev` | Admin | Rotate to the previous track. |
| `/extend <seconds>` | Admin | Extend the current track's timer. |
| `/interval <seconds>` | Admin | Set the rotation interval (minimum 30s). |
| `/shuffle on\|off` | Admin | Toggle track shuffle. |
| `/democracy on\|off` | Admin | Toggle public `/skip` voting. |
| `/mode infinite\|circuit\|dropout\|survival\|auto` | Admin | Show or override the game mode. |
| `/playlist [name]` | Admin | Show or switch the active playlist (server mode; playlists are managed by the orchestrator). |
| `/maxplayers <number>` | Admin | Set the room player limit (minimum 2). |
| `/private [name]` | Admin | Make the room private, or recreate it with a join name. |
| `/public` | Admin | Make the room public (visible in the lobby list). |
| `/promote <username>` | Admin | Hand lobby-host to another player. |
| `/kick <player_name>` | Admin | Kick a player from the room. |
| `/maintenance [minutes\|cancel]` | Admin | Schedule a shutdown (server mode). |
| `/reloadtheme` | Admin | Reload the chat color theme from `chat_theme.json`. |

`TBD(build-release-pipeline)`: `/version` is planned but not implemented yet — until it ships,
include the exact zip filename or git commit you're running when reporting a bug.

`TBD(client-lifecycle-commands)`: `/start`, `/stop`, `/pause`, `/resume` (see previous section)
are not in the table above because they don't exist in the codebase yet.

Note for player-mode users: some of the descriptions above, and some log lines the plugin
writes, still assume a server operator (e.g. "playlists are managed by the orchestrator").
Cleaning that language up for a player audience is tracked as a plugin-half item of this same
feature, gated on R3 (`docs/features/doing/player-onboarding-ux.md`) — if a message doesn't
make sense in your context, that's a known gap, not something you're doing wrong.

## Where are the logs

This trips up experienced users, not just newcomers: the plugin's **runtime** output (chat,
commands, rotation, errors) goes to the *game's* log, not to BepInEx's own log file. BepInEx's
log only records the plugin's bootstrap/load line.

- **Plugin runtime log (the one you want for bug reports):**
  - Linux: `~/.config/unity3d/LuGus Studios/Liftoff/Player.log`
  - Windows: `TBD(windows-compatibility)` — expected at
    `%USERPROFILE%\AppData\LocalLow\LuGus Studios\Liftoff\Player.log` (standard Unity
    `Application.persistentDataPath` layout on Windows), not yet confirmed on a real install.
  - Look for lines starting `[Unity Log] [AutoLobbyPlugin]`.
- **BepInEx bootstrap log** (confirms the DLL loaded at all, nothing more):
  - Linux: `<Liftoff install>/BepInEx/LogOutput.log`
  - Windows: same relative path under the game install folder — `TBD(windows-compatibility)`
    for final confirmation.

## Reporting a bug

Include:

1. The plugin runtime log excerpt from around when the problem happened (see above — not the
   BepInEx log by itself).
2. The exact zip filename or commit you're running (until `/version` ships —
   `TBD(build-release-pipeline)`).
3. Player or server package, and Windows or Linux.
4. What you typed, what you expected, and what actually happened.

## Upgrading

Close the game first. `TBD(windows-compatibility)`: on Windows, the running game process locks
the loaded DLL — you cannot overwrite `LiftoffAutoLobby.dll` while the game is open. Replace the
DLL with the new version, then relaunch. Your `tracks_to_rotate.txt` and other config files are
untouched by an upgrade — they aren't part of the DLL.

## Uninstalling

Delete `LiftoffAutoLobby.dll` from `BepInEx/plugins/`. That's it — the plugin doesn't write
anything outside that folder and leaves no other trace. (If you want BepInEx itself gone too,
that's a separate step: delete the `BepInEx/` folder and its `winhttp.dll` /
`doorstop_config.ini` from the game root, and remove any Proton launch-option override you
added.)

## Troubleshooting

- **Nothing happens when I join a room.** Confirm BepInEx loaded the plugin at all: check the
  BepInEx bootstrap log for a `Liftoff Auto Lobby` load line. If it's not there, the DLL isn't
  in the right folder, or BepInEx itself isn't hooked in (Windows: check the Proton launch
  option if applicable).
- **The plugin runtime log says `tracks_to_rotate.txt not found. Using default values.`** — the
  file isn't in `BepInEx/plugins/` (not a subfolder), or the name is misspelled. Harmless to the
  game itself; it just means rotation has nothing to rotate through.
- **`admin_ids.txt not found — no admins configured.`** — expected and harmless if you haven't
  set up server-mode admins; you need this file today because client-mode implicit-host-admin
  (`TBD(plugin-mode-split)`) hasn't shipped yet.
- **A command says nothing, or nothing visibly changes.** Run `/help` to confirm you're allowed
  to run it in your current context — commands are supposed to explain why they refuse (e.g.
  "you're not the host") rather than silently no-op. If a command silently does nothing instead
  of explaining why, that's a bug — please report it (see above).
- **Known issue:** several server-mode behaviors are implemented but not yet live-verified on a
  fresh install by anyone other than the project's own dev/server setup — check
  `docs/features/doing/` (or the release notes, once tagged) for the current list before
  relying on an edge case.

## For server operators

The server package runs the same plugin DLL in `server` mode: a Python orchestrator manages
Steam login, launch/recovery, and playlists, and writes the plugin's config as text files in
`BepInEx/plugins/` instead of you typing chat commands. See
[`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) for the full dev/operator setup (`build.sh`,
`infra/setup_bot.sh`, Docker), and `AGENTS.md` for the plugin⇄orchestrator file protocol if
you're scripting against it.

## Contributing / building from source

See [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md).

## License

`TBD(build-release-pipeline)`: a licensing decision for the game DLLs currently referenced by
the build (`plugin/libs/`) has been made (keep them out of the public tree; CI restores them
from a private source) but not yet executed — see
`docs/features/todo/build-release-pipeline.md` §2. This section will state the project's own
license once that's resolved.
