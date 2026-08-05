"""Shared fixtures for the dashboard tests.

Everything is built against a throwaway project tree: a fake ``config/`` (lobby_config +
playlists + master track list), a fake BepInEx ``plugins/`` directory, and a fake log
directory. Nothing here touches the real repo config, the real game install, or the
live bot — the whole point of the D5 service layer is that all three are just paths.
"""

import json

import pytest

from dashboard.control.protocol import ProtocolDir
from dashboard.control.settings import DashboardSettings

TOKEN = "test-token"

MASTER_TRACKS = {
    "Bando City": {"official": ["BC Track 0", "BC Track 1"], "local": ["Local Only"]},
    "The Green": {"official": ["Green Track 0"], "workshop": ["[Honk] Backtrack"]},
}

PLAYLISTS = {
    "all_official_races": [{"environment": "*", "track": "*", "mode": "Infinite Race"}],
    "bando_only": [{"environment": "Bando City", "track": "*", "mode": "Race"}],
}


@pytest.fixture
def token():
    """The shared token the `client` fixture authenticates with."""
    return TOKEN


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for var in ("FPV_LOG_DIR", "FPV_PLUGINS_DIR", "FPV_GAME_DIR", "FPV_PLAYER_LOG",
                "FPV_DASHBOARD_TOKEN", "FPV_DASHBOARD_ALLOW_RESTART", "FPV_BOT_CONTAINER"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def project(tmp_path):
    """A fake repo checkout + game install the control plane can be pointed at."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    game_dir = tmp_path / "game" / "Liftoff"
    plugins_dir = game_dir / "BepInEx" / "plugins"
    plugins_dir.mkdir(parents=True)
    (game_dir / "Liftoff.x86_64").write_text("")
    (game_dir / "BepInEx" / "LogOutput.log").write_text("[Info   :AutoLobbyPlugin] booted\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    (config_dir / "lobby_config.json").write_text(json.dumps({
        "liftoff_path": str(game_dir / "Liftoff.x86_64"),
        "display": ":99",
        "lobby_name": "Procedural Loop Room",
        "log_dir": str(log_dir),
    }))
    (config_dir / "playlists.json").write_text(json.dumps(PLAYLISTS))
    (config_dir / "master_tracks_list.json").write_text(json.dumps(MASTER_TRACKS))

    return {
        "root": str(tmp_path),
        "config_dir": str(config_dir),
        "plugins_dir": str(plugins_dir),
        "game_dir": str(game_dir),
        "log_dir": str(log_dir),
        "playlists_path": str(config_dir / "playlists.json"),
        "master_path": str(config_dir / "master_tracks_list.json"),
    }


@pytest.fixture
def protocol(project):
    return ProtocolDir(project["plugins_dir"])


@pytest.fixture
def settings(project):
    return DashboardSettings(token=TOKEN, project_dir=project["root"])


@pytest.fixture
def client(project, settings):
    from fastapi.testclient import TestClient

    from dashboard.api import create_app

    app = create_app(settings, plugins_dir=project["plugins_dir"], log_dir=project["log_dir"])
    with TestClient(app) as test_client:
        test_client.headers.update({"X-Auth-Token": TOKEN})
        yield test_client


@pytest.fixture
def anon_client(project, settings):
    """The same app without the auth header pre-set, for the auth tests."""
    from fastapi.testclient import TestClient

    from dashboard.api import create_app

    app = create_app(settings, plugins_dir=project["plugins_dir"], log_dir=project["log_dir"])
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def write_event(project):
    """Append a JSONL event to today's daily file, as either producer would."""
    from dashboard.control import events as events_mod

    def _write(name, ts=None, source="plugin", **fields):
        record = {"ts": ts or events_mod.utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "source": source, "event": name}
        record.update(fields)
        path = events_mod.daily_path(project["log_dir"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record

    return _write
