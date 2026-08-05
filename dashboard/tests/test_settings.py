"""Tests for dashboard.control.settings (decision D2: one shared token, set in config).

The load-bearing behaviours: the token has no default (a dashboard that can rewrite the
rotation must not come up open), env beats config, and the container-restart action
(D3) stays off unless the operator *both* flips the flag and names a command.
"""

from dashboard.control.settings import DEFAULT_HOST, DEFAULT_PORT, load_settings


def load(env=None, config=None, project_dir="/tmp/project"):
    return load_settings(project_dir=project_dir, env=env or {}, config=config or {})


class TestToken:
    def test_absent_by_default(self):
        assert load().token is None

    def test_read_from_the_lobby_config_dashboard_section(self):
        assert load(config={"dashboard": {"token": "s3cret"}}).token == "s3cret"

    def test_env_beats_config(self):
        settings = load(env={"FPV_DASHBOARD_TOKEN": "from-env"},
                        config={"dashboard": {"token": "from-config"}})
        assert settings.token == "from-env"

    def test_token_is_never_exposed_to_the_browser(self):
        assert "token" not in load(config={"dashboard": {"token": "s3cret"}}).public_dict()


class TestProjectDir:
    def test_env_override_points_at_another_checkout(self):
        settings = load_settings(project_dir=None, env={"FPV_PROJECT_DIR": "/srv/fpv"}, config={})
        assert settings.project_dir == "/srv/fpv"

    def test_defaults_to_the_repo_root(self):
        import os

        settings = load_settings(project_dir=None, env={}, config={})
        assert os.path.isdir(os.path.join(settings.project_dir, "dashboard"))


class TestBind:
    def test_defaults_are_loopback(self):
        settings = load()
        assert (settings.host, settings.port) == (DEFAULT_HOST, DEFAULT_PORT)

    def test_config_and_env_overrides(self):
        assert load(config={"dashboard": {"host": "0.0.0.0", "port": 9000}}).port == 9000
        assert load(env={"FPV_DASHBOARD_PORT": "9100"}).port == 9100

    def test_a_nonsense_port_falls_back_instead_of_crashing(self):
        assert load(env={"FPV_DASHBOARD_PORT": "eight thousand"}).port == DEFAULT_PORT

    def test_poll_interval_is_configurable(self):
        assert load(env={"FPV_DASHBOARD_POLL_INTERVAL": "0.25"}).poll_interval == 0.25


class TestContainerRestart:
    def test_disabled_by_default(self):
        settings = load()
        assert settings.allow_container_restart is False
        assert settings.restart_command == []
        assert settings.restart_available is False

    def test_flag_alone_is_not_enough(self):
        # No container named -> nothing to restart -> the endpoint must stay refused,
        # rather than defaulting to some container name and restarting the wrong one.
        assert load(env={"FPV_DASHBOARD_ALLOW_RESTART": "true"}).restart_available is False

    def test_flag_plus_container_name_enables_it(self):
        settings = load(env={"FPV_DASHBOARD_ALLOW_RESTART": "1", "FPV_BOT_CONTAINER": "fpv-bot"})
        assert settings.restart_available is True
        assert settings.restart_command == ["docker", "restart", "fpv-bot"]

    def test_explicit_command_wins_over_the_container_name(self):
        settings = load(env={"FPV_DASHBOARD_ALLOW_RESTART": "yes"},
                        config={"dashboard": {"restart_command": ["systemctl", "restart", "fpvbot"]}})
        assert settings.restart_command == ["systemctl", "restart", "fpvbot"]

    def test_config_can_enable_it_without_env(self):
        settings = load(config={"dashboard": {"allow_container_restart": True,
                                              "container_name": "fpv-bot"}})
        assert settings.restart_available is True

    def test_public_dict_advertises_availability(self):
        settings = load(config={"dashboard": {"allow_container_restart": True,
                                              "container_name": "fpv-bot"}})
        assert settings.public_dict()["restart_available"] is True
        assert load().public_dict()["restart_command"] is None
