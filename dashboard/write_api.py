"""Write-side routes: playlist CRUD and bot controls.

Every route here is a thin translation of an HTTP request into a ``dashboard.control``
call. Nothing in this module opens a protocol file, resolves a playlist or shells out —
that all lives in the service layer the orchestrator shares (decision D5), which is what
keeps the two writers obeying one set of rules.

Registered by ``dashboard.api.create_app`` so there is still exactly one app factory.
"""

from typing import Optional

from fastapi import Body, HTTPException
from pydantic import BaseModel, Field

from .control import paths as paths_mod
from .control import playlist_store, restart
from .control.playlists import MasterTracksMissingError, PlaylistError, resolve_and_write_playlist
from .control.protocol import RESET_ONLY, WRITABLE

# The one v1 control that cannot be implemented from the file protocol. See the feature
# doc's "Spec conflict" section: /skip sets an in-memory `skipRequested` flag inside the
# plugin and there is no file it polls for it, so any endpoint here would either lie
# (write a file nothing reads) or race (temporarily shrink rotation_interval.txt and hope
# the tick lands in the window). The honest answer is 501 plus the exact plugin change
# that would fix it.
SKIP_NOT_IMPLEMENTED = (
    "Skip-now needs a plugin change and is not implemented. The plugin's /skip sets an "
    "in-memory flag (skipRequested in Plugin.Commands/SkipCommand.cs); no protocol file "
    "carries it, so the dashboard cannot request one without the plugin polling for it. "
    "Fix: have HandleGameRoom check for a `skip_now.txt` in the plugins dir, set "
    "skipRequested and delete the file. Until then, use /skip in game chat."
)


class PlaylistBody(BaseModel):
    items: list = Field(default_factory=list)
    force: bool = False


class ActivateBody(BaseModel):
    # Resolving immediately is the default: the operator pressed a button and expects the
    # rotation to change. The orchestrator's own playlist_name.txt watcher will also
    # notice and resolve within a second -- that is idempotent (same inputs, same output
    # file, same resets), so the duplicate costs nothing and covers the case where the
    # dashboard is running without an orchestrator.
    resolve: bool = True


class IntervalBody(BaseModel):
    seconds: int = Field(ge=5, le=86400)


class LobbyBody(BaseModel):
    name: Optional[str] = None
    private: Optional[bool] = None
    max_players: Optional[int] = Field(default=None, ge=1, le=64)


class FlagBody(BaseModel):
    enabled: bool


class GameModeBody(BaseModel):
    # Explicitly nullable: `{"mode": null}` is how the override is CLEARED (the plugin
    # treats an absent override_game_mode.txt as "use each track's own mode").
    mode: Optional[str] = None


class RotationBody(BaseModel):
    paused: Optional[bool] = None
    engaged: Optional[bool] = None


def register(app, ctx, auth):
    def playlists_path():
        return paths_mod.playlists_path(ctx.project_dir)

    def master_path():
        return paths_mod.master_tracks_path(ctx.project_dir)

    def publish_playlist_names(protocol, data):
        """Keep ``available_playlists.txt`` (what the in-game /playlist command lists) in
        step with playlists.json. Same file the orchestrator writes at startup, same
        writer."""
        protocol.set_available_playlists(list(data.keys()))

    # --- playlists ------------------------------------------------------------

    @app.get("/api/playlists", dependencies=auth)
    def get_playlists():
        data, findings, master_available = playlist_store.lint_all(
            playlists_path(), master_path(), ctx.project_dir)
        return {
            "playlists": data,
            "findings": findings,
            "master_tracks_available": master_available,
            "active": ctx.protocol().read_text("playlist_name.txt"),
            "protected": sorted(playlist_store.PROTECTED_PLAYLISTS),
        }

    @app.post("/api/playlists/{name}/validate", dependencies=auth)
    def validate_playlist(name: str, body: PlaylistBody):
        master_data = playlist_store.load_master(master_path(), ctx.project_dir)
        findings = playlist_store.validate_playlist(name, body.items, master_data)
        return {
            "name": name,
            "findings": findings,
            "blocking": len(playlist_store.blocking(findings)),
            "warnings": len(playlist_store.warnings(findings)),
            "master_tracks_available": master_data is not None,
        }

    @app.put("/api/playlists/{name}", dependencies=auth)
    def put_playlist(name: str, body: PlaylistBody):
        try:
            data, findings = playlist_store.upsert_playlist(
                name, body.items, force=body.force,
                playlists_path=playlists_path(), master_path=master_path(),
                project_dir=ctx.project_dir)
        except playlist_store.PlaylistStoreError as e:
            raise HTTPException(status_code=400,
                                detail={"message": str(e), "findings": e.findings})
        publish_playlist_names(ctx.protocol(), data)
        return {"name": name, "saved": True, "findings": findings,
                "playlists": sorted(data.keys())}

    @app.delete("/api/playlists/{name}", dependencies=auth)
    def delete_playlist(name: str):
        protocol = ctx.protocol()
        try:
            data = playlist_store.delete_playlist(
                name, active_playlist=protocol.read_text("playlist_name.txt"),
                playlists_path=playlists_path(), project_dir=ctx.project_dir)
        except playlist_store.PlaylistStoreError as e:
            raise HTTPException(status_code=400, detail={"message": str(e), "findings": e.findings})
        publish_playlist_names(protocol, data)
        return {"name": name, "deleted": True, "playlists": sorted(data.keys())}

    @app.post("/api/playlists/{name}/activate", dependencies=auth)
    def activate_playlist(name: str, body: ActivateBody = Body(default=ActivateBody())):
        data = playlist_store.load_playlists(playlists_path(), ctx.project_dir)
        if name not in data:
            raise HTTPException(status_code=404, detail=f"No such playlist: {name}")

        protocol = ctx.protocol()
        publish_playlist_names(protocol, data)
        protocol.set_playlist_name(name)

        resolved = None
        if body.resolve:
            try:
                tracks = resolve_and_write_playlist(
                    name, protocol.read_flag("shuffle_mode.txt"),
                    protocol.path("tracks_to_rotate.txt"),
                    playlists_path=playlists_path(), master_list_path=master_path())
                resolved = len(tracks) if tracks is not None else None
            except MasterTracksMissingError as e:
                # The playlist name is already written, so the orchestrator will retry the
                # resolution itself; report the real reason rather than a generic 500.
                raise HTTPException(status_code=409, detail=(
                    f"{e} — it is generated from a live game install by gather_tracks.py, "
                    f"so the bot must have run at least once on this machine."))
            except PlaylistError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return {"active": name, "resolved_tracks": resolved}

    # --- bot controls ---------------------------------------------------------

    @app.get("/api/control/info", dependencies=auth)
    def control_info():
        return {
            "settings": ctx.settings.public_dict(),
            "skip_supported": False,
            "skip_reason": SKIP_NOT_IMPLEMENTED,
            "writable_files": WRITABLE,
            "plugin_owned_files": RESET_ONLY,
        }

    @app.post("/api/control/interval", dependencies=auth)
    def set_interval(body: IntervalBody):
        ctx.protocol().set_rotation_interval(body.seconds)
        return {"rotation_interval_s": body.seconds}

    @app.post("/api/control/lobby", dependencies=auth)
    def set_lobby(body: LobbyBody):
        protocol = ctx.protocol()
        changed = {}
        if body.name is not None:
            if not body.name.strip():
                raise HTTPException(status_code=400, detail="Lobby name cannot be empty.")
            protocol.set_lobby_name(body.name.strip())
            changed["lobby_name"] = body.name.strip()
        if body.private is not None:
            protocol.set_room_private(body.private)
            changed["room_private"] = body.private
        if body.max_players is not None:
            protocol.set_max_players(body.max_players)
            changed["max_players"] = body.max_players
        if not changed:
            raise HTTPException(status_code=400, detail="Nothing to change.")
        # The plugin applies room settings when it next (re)creates or updates the room,
        # not instantly -- say so rather than letting the UI imply an immediate effect.
        return {"changed": changed, "applies": "on the next room update/recreate"}

    @app.post("/api/control/auto-start", dependencies=auth)
    def set_auto_start(body: FlagBody):
        ctx.protocol().set_auto_start(body.enabled)
        return {"auto_start": body.enabled}

    @app.post("/api/control/democracy", dependencies=auth)
    def set_democracy(body: FlagBody):
        ctx.protocol().set_democracy_mode(body.enabled)
        return {"democracy_mode": body.enabled}

    @app.post("/api/control/shuffle", dependencies=auth)
    def set_shuffle(body: FlagBody):
        protocol = ctx.protocol()
        protocol.set_shuffle_mode(body.enabled)
        # Clear the plugin-owned deal so the next read re-deals against the current
        # rotation instead of resuming a stale permutation -- the same invalidation the
        # in-game /shuffle command performs, and the only mutation of that file the
        # control plane is allowed to make.
        protocol.clear_shuffle_order()
        return {"shuffle_mode": body.enabled}

    @app.post("/api/control/game-mode", dependencies=auth)
    def set_game_mode(body: GameModeBody):
        applied = ctx.protocol().set_override_game_mode(body.mode)
        return {"override_game_mode": body.mode if applied and body.mode else None}

    @app.post("/api/control/rotation", dependencies=auth)
    def set_rotation(body: RotationBody):
        protocol = ctx.protocol()
        changed = {}
        if body.paused is not None:
            protocol.set_rotation_paused(body.paused)
            changed["rotation_paused"] = body.paused
        if body.engaged is not None:
            protocol.set_rotation_engaged(body.engaged)
            changed["rotation_engaged"] = body.engaged
        if not changed:
            raise HTTPException(status_code=400, detail="Nothing to change.")
        return {"changed": changed}

    @app.post("/api/control/maintenance", dependencies=auth)
    def set_maintenance(body: FlagBody):
        # Presence-based by design: the plugin schedules a 3-minute shutdown when the file
        # appears and cancels (announcing it) when it disappears.
        ctx.protocol().set_maintenance(body.enabled)
        return {"maintenance_active": body.enabled,
                "note": "The bot announces a 3-minute shutdown; clearing it cancels."}

    @app.post("/api/control/skip", dependencies=auth)
    def skip_now():
        raise HTTPException(status_code=501, detail=SKIP_NOT_IMPLEMENTED)

    @app.post("/api/control/restart", dependencies=auth)
    def restart_container():
        try:
            result = restart.restart_bot(ctx.settings)
        except restart.RestartUnavailableError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except restart.RestartFailedError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"restarted": True, **result}
