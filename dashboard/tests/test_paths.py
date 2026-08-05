"""Tests for dashboard.control.paths — the single answer to "where is X".

The interesting cases are the ones where the same checkout runs in three places (dev
user's home, the fpv_bot account, the Docker image), which is why every resolver has an
env override and a derivation, and why the liftoff-path auto-correction exists at all.
"""

import json
import os

import pytest

from dashboard.control import paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("FPV_LOG_DIR", "FPV_PLUGINS_DIR", "FPV_GAME_DIR", "FPV_PLAYER_LOG"):
        monkeypatch.delenv(var, raising=False)


class TestRepoLayout:
    def test_repo_root_is_the_parent_of_the_dashboard_package(self):
        root = paths.repo_root()
        assert os.path.isdir(os.path.join(root, "dashboard", "control"))
        assert os.path.isdir(os.path.join(root, "orchestrator"))

    def test_config_paths_hang_off_the_project_dir(self, tmp_path):
        assert paths.playlists_path(str(tmp_path)) == str(tmp_path / "config" / "playlists.json")
        assert paths.master_tracks_path(str(tmp_path)) == str(tmp_path / "config" / "master_tracks_list.json")
        assert paths.lobby_config_path(str(tmp_path)) == str(tmp_path / "config" / "lobby_config.json")


class TestLobbyConfig:
    def test_missing_config_raises_for_the_caller_to_decide(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            paths.load_lobby_config(str(tmp_path))

    def test_or_empty_variant_degrades_instead(self, tmp_path):
        assert paths.load_lobby_config_or_empty(str(tmp_path)) == {}

    def test_or_empty_variant_also_survives_malformed_json(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "lobby_config.json").write_text("{not json")
        assert paths.load_lobby_config_or_empty(str(tmp_path)) == {}

    def test_valid_config_is_parsed(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "lobby_config.json").write_text(json.dumps({"display": ":99"}))
        assert paths.load_lobby_config(str(tmp_path)) == {"display": ":99"}


class TestLiftoffPath:
    def test_existing_path_is_returned_untouched(self, tmp_path):
        exe = tmp_path / "Liftoff.x86_64"
        exe.write_text("")
        assert paths.resolve_liftoff_path({"liftoff_path": str(exe)}) == str(exe)

    def test_empty_config_yields_empty_path(self):
        assert paths.resolve_liftoff_path({}) == ""

    def test_other_users_home_is_rewritten_when_the_alternative_exists(self, monkeypatch):
        # Simulates lobby_config.json's committed /home/fpv_bot/... path being opened by
        # the dev user, whose own copy of the install does exist. Real /home/<user> trees
        # can't be created in a test, so existence is stubbed for exactly one path.
        import getpass

        configured = "/home/fpv_bot/.steam/x/Liftoff/Liftoff.x86_64"
        expected = "/home/devuser/.steam/x/Liftoff/Liftoff.x86_64"
        monkeypatch.setattr(getpass, "getuser", lambda: "devuser")
        monkeypatch.setattr(os.path, "exists", lambda p: p == expected)
        assert paths.resolve_liftoff_path({"liftoff_path": configured}) == expected

    def test_other_users_home_is_kept_when_no_alternative_exists(self, monkeypatch):
        import getpass

        configured = "/home/fpv_bot/.steam/x/Liftoff/Liftoff.x86_64"
        monkeypatch.setattr(getpass, "getuser", lambda: "devuser")
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        assert paths.resolve_liftoff_path({"liftoff_path": configured}) == configured

    def test_nonexistent_non_home_path_is_returned_as_is(self):
        assert paths.resolve_liftoff_path({"liftoff_path": "/opt/nope/Liftoff.x86_64"}) \
            == "/opt/nope/Liftoff.x86_64"


class TestDerivedDirectories:
    def test_game_dir_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("FPV_GAME_DIR", "/steam/Liftoff")
        assert paths.resolve_game_dir({"liftoff_path": "/elsewhere/Liftoff.x86_64"}) == "/steam/Liftoff"

    def test_game_dir_derives_from_the_executable(self):
        assert paths.resolve_game_dir({"liftoff_path": "/games/Liftoff/Liftoff.x86_64"}) == "/games/Liftoff"

    def test_game_dir_is_none_without_a_configured_path(self, tmp_path):
        assert paths.resolve_game_dir({}, str(tmp_path)) is None

    def test_plugins_dir_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FPV_PLUGINS_DIR", str(tmp_path))
        assert paths.resolve_plugins_dir({}) == str(tmp_path)

    def test_plugins_dir_derives_from_the_game_dir(self):
        assert paths.resolve_plugins_dir({"liftoff_path": "/games/Liftoff/Liftoff.x86_64"}) \
            == "/games/Liftoff/BepInEx/plugins"

    def test_plugins_dir_is_none_when_unresolvable(self, tmp_path):
        assert paths.resolve_plugins_dir({}, str(tmp_path)) is None

    def test_bepinex_log_path(self):
        assert paths.bepinex_log_path({"liftoff_path": "/games/Liftoff/Liftoff.x86_64"}) \
            == "/games/Liftoff/BepInEx/LogOutput.log"


class TestPlayerLog:
    def test_env_override_is_the_only_candidate(self, monkeypatch):
        monkeypatch.setenv("FPV_PLAYER_LOG", "/tmp/Player.log")
        assert paths.player_log_candidates({"liftoff_path": "/games/Liftoff/Liftoff.x86_64"}) \
            == ["/tmp/Player.log"]

    def test_host_layout_derives_the_home_from_the_steam_marker(self):
        config = {"liftoff_path": "/home/fpv_bot/.steam/debian-installation/steamapps/"
                                  "common/Liftoff/Liftoff.x86_64"}
        candidates = paths.player_log_candidates(config)
        assert candidates[0] == ("/home/fpv_bot/.config/unity3d/LuGus Studios/Liftoff/Player.log")

    def test_docker_layout_derives_the_home_from_the_game_dirs_parent(self):
        candidates = paths.player_log_candidates({"liftoff_path": "/steam/Liftoff/Liftoff.x86_64"})
        assert candidates == ["/steam/.config/unity3d/LuGus Studios/Liftoff/Player.log"]

    def test_no_game_dir_means_no_candidates(self, tmp_path):
        assert paths.player_log_candidates({}, str(tmp_path)) == []

    def test_player_log_path_prefers_an_existing_candidate(self, tmp_path):
        home = tmp_path / "steam"
        log = home / ".config" / "unity3d" / "LuGus Studios" / "Liftoff" / "Player.log"
        log.parent.mkdir(parents=True)
        log.write_text("hello")
        config = {"liftoff_path": str(home / "Liftoff" / "Liftoff.x86_64")}
        assert paths.player_log_path(config) == str(log)


class TestLogDirDelegation:
    def test_delegates_to_the_orchestrators_resolver(self, tmp_path, monkeypatch):
        # Precedence is event_log.resolve_log_dir's, not re-implemented here: env wins.
        monkeypatch.setenv("FPV_LOG_DIR", str(tmp_path / "vol"))
        assert paths.resolve_log_dir({"log_dir": "ignored"}) == str(tmp_path / "vol")

    def test_config_key_is_honoured(self, tmp_path):
        assert paths.resolve_log_dir({"log_dir": str(tmp_path)}) == str(tmp_path)

    def test_default_is_the_repo_logs_dir(self, tmp_path):
        assert paths.resolve_log_dir({}, str(tmp_path)) == str(tmp_path / "logs")

    def test_returns_none_when_structured_logging_is_unavailable(self, monkeypatch):
        from dashboard.control import eventlog

        monkeypatch.setattr(eventlog, "EVENT_LOG_AVAILABLE", False)
        assert paths.resolve_log_dir({}) is None
