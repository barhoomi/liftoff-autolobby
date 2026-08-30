"""The bot control plane — the one implementation of playlist resolution and of every
write to the plugin's plain-text protocol files (feature decision D5).

Consumers:

- ``orchestrator/run_headless_lobby.py`` — imports this package for its startup
  configuration writes and its playlist resolution. It no longer contains any of that
  logic itself.
- ``dashboard.api`` — the web app; every control it exposes funnels through here, so
  both processes obey the same ownership rules and the same write locking.

Nothing in this package imports FastAPI; it is plain Python + the repo's own
``trackcheck`` and ``orchestrator/event_log`` modules.
"""

from .bootstrap import (
    DEFAULT_TIMEOUT_SECONDS as BOOTSTRAP_DEFAULT_TIMEOUT_SECONDS,
    TrackBootstrap,
    bootstrap_timeout,
    master_list_has_tracks,
    track_dump_ready,
)
from .eventlog import EVENT_LOG_AVAILABLE, NullLogger, make_event_logger
from .paths import (
    bepinex_log_path,
    config_dir,
    lobby_config_path,
    load_lobby_config,
    load_lobby_config_or_empty,
    master_tracks_path,
    player_log_candidates,
    player_log_path,
    playlists_path,
    repo_root,
    resolve_game_dir,
    resolve_liftoff_path,
    resolve_log_dir,
    resolve_plugins_dir,
)
from .playlists import (
    MasterTracksMissingError,
    PlaylistError,
    PlaylistNotFoundError,
    cross_validate_tracks,
    load_track_mode_availability,
    resolve_and_write_playlist,
    round_robin_shuffle_by_environment,
)
from .protocol import (
    READ_ONLY,
    RESET_ONLY,
    WRITABLE,
    ProtocolDir,
    ProtocolOwnershipError,
    parse_workshop_download_result,
)
from .shuffle_order import compute_tracks_signature, read_active_order
from .workshop_download import (
    DownloadOutcome,
    download_workshop_item,
    wait_for_result,
)

__all__ = [
    "BOOTSTRAP_DEFAULT_TIMEOUT_SECONDS",
    "EVENT_LOG_AVAILABLE",
    "MasterTracksMissingError",
    "NullLogger",
    "PlaylistError",
    "PlaylistNotFoundError",
    "DownloadOutcome",
    "ProtocolDir",
    "ProtocolOwnershipError",
    "READ_ONLY",
    "RESET_ONLY",
    "TrackBootstrap",
    "WRITABLE",
    "bepinex_log_path",
    "bootstrap_timeout",
    "config_dir",
    "compute_tracks_signature",
    "download_workshop_item",
    "cross_validate_tracks",
    "lobby_config_path",
    "load_lobby_config",
    "load_lobby_config_or_empty",
    "load_track_mode_availability",
    "make_event_logger",
    "master_list_has_tracks",
    "master_tracks_path",
    "parse_workshop_download_result",
    "player_log_candidates",
    "player_log_path",
    "playlists_path",
    "read_active_order",
    "repo_root",
    "resolve_and_write_playlist",
    "resolve_game_dir",
    "resolve_liftoff_path",
    "resolve_log_dir",
    "resolve_plugins_dir",
    "round_robin_shuffle_by_environment",
    "track_dump_ready",
    "wait_for_result",
]
