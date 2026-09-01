"""CLI: pull a Steam Workshop track into the RUNNING bot, no restart.

    python3 orchestrator/download_workshop_item.py 1234567890

Thin wrapper -- every step lives in ``dashboard/control/workshop_download.py`` (the
control plane owns protocol writes; see that module for the request/validate/quarantine/
re-gather flow). This file only answers "which install, which playlist, which log dir",
using the same resolvers the orchestrator itself uses, and prints the outcome.

Requires the game to be running with the plugin loaded: the download happens *inside* the
game process (that is the whole point -- see
``docs/features/done/workshop-ingame-download-spike.md`` Q4). With the game down, this
exits non-zero after the wait bound with ``watcher_timeout``; use the steamcmd path
(``workshop-steamcmd-install.md``) for offline installs.
"""

import argparse
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dashboard.control import (  # noqa: E402  (import needs the bootstrap above)
    ProtocolDir,
    download_workshop_item,
    load_lobby_config_or_empty,
    make_event_logger,
    resolve_game_dir,
    resolve_plugins_dir,
)
from dashboard.control.workshop_download import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workshop_id", help="Steam Workshop published file id (decimal).")
    parser.add_argument("--playlist", default=None,
                        help="Playlist to re-resolve after the download. Default: whatever "
                             "playlist_name.txt currently holds; pass an empty string to "
                             "refresh the track database without rewriting the rotation.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="Seconds to wait for the plugin's result file "
                             "(default: %(default)s, just past the plugin's own 120s budget).")
    args = parser.parse_args(argv)

    config = load_lobby_config_or_empty(_PROJECT_ROOT)
    plugins_dir = resolve_plugins_dir(config, _PROJECT_ROOT)
    if not plugins_dir:
        print("ERROR: could not resolve the BepInEx plugins directory "
              "(set liftoff_path in config/lobby_config.json, or FPV_PLUGINS_DIR).")
        return 2

    protocol = ProtocolDir(plugins_dir)
    logger = make_event_logger(config, _PROJECT_ROOT)

    playlist_name = args.playlist
    if playlist_name is None:
        playlist_name = protocol.read_text("playlist_name.txt", "") or ""
    # "custom" is the orchestrator's marker for "no named playlist, use the file as-is"
    # (run_headless_lobby.py writes it when --playlist is omitted): resolving it would
    # raise PlaylistNotFoundError, so treat it the same as an explicit empty string.
    if playlist_name == "custom":
        playlist_name = ""

    outcome = download_workshop_item(
        args.workshop_id,
        protocol,
        game_dir=resolve_game_dir(config, _PROJECT_ROOT),
        playlist_name=playlist_name or None,
        tracks_file=protocol.path("tracks_to_rotate.txt"),
        shuffle=protocol.read_flag("shuffle_mode.txt"),
        logger=logger,
        timeout=args.timeout,
        project_dir=_PROJECT_ROOT,
    )
    print(outcome.summary())
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
