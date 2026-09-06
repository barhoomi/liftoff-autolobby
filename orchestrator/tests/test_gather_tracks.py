import json
import os
import shutil

import pytest

import gather_tracks
from gather_tracks import gather_tracks_and_races, is_tutorial, normalize_env

TRACKCHECK_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "trackcheck", "tests", "fixtures")


class TestNormalizeEnv:
    def test_known_mapping(self):
        assert normalize_env("TheDrawingBoard") == "The Drawing Board"
        assert normalize_env("BardwellsYard") == "Bardwell's Yard"

    def test_already_normalized_name_passes_through(self):
        assert normalize_env("The Drawing Board") == "The Drawing Board"

    def test_unmapped_camel_case_falls_back_to_spaced_words(self):
        # Not in ENV_MAPPING -- exercises the regex fallback rather than the lookup table.
        assert normalize_env("SomeBrandNewEnvironment") == "Some Brand New Environment"

    def test_empty_or_none_is_unknown(self):
        assert normalize_env(None) == "Unknown"
        assert normalize_env("") == "Unknown"


class TestIsTutorial:
    @pytest.mark.parametrize("name", ["Tutorial - Basics", "tutorial01", "Learning Curve", "learning the ropes"])
    def test_tutorial_and_learning_prefixes(self, name):
        assert is_tutorial(name) is True

    @pytest.mark.parametrize("name", ["My Custom Track", "Advanced Loop", None, ""])
    def test_non_tutorial_names(self, name):
        assert is_tutorial(name) is False


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """A repo + Steam layout shaped exactly like the container's, in tmp_path.

    ``gather_tracks_and_races()`` derives both its config path and its master-list path
    from the module's own ``__file__``, so pointing that at a temp "orchestrator/"
    directory is what redirects every read and write into the sandbox -- in particular it
    keeps the test from writing the real (gitignored, generated) config/
    master_tracks_list.json, whose presence would silently switch run_tests.sh's playlist
    lint on.
    """
    monkeypatch.setattr(gather_tracks, "__file__",
                        str(tmp_path / "orchestrator" / "gather_tracks.py"))
    # expanduser() candidates (custom Tracks/Races) and the workshop env override must
    # not reach into the developer's real home / environment.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FPV_WORKSHOP_CONTENT_DIR", raising=False)

    steamapps = tmp_path / "steam" / "steamapps"
    game_dir = steamapps / "common" / "Liftoff"
    game_dir.mkdir(parents=True)
    liftoff_path = game_dir / "Liftoff.x86_64"
    liftoff_path.write_text("")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "lobby_config.json").write_text(
        json.dumps({"liftoff_path": str(liftoff_path)}))

    return {
        "tmp_path": tmp_path,
        "steamapps": steamapps,
        "game_dir": game_dir,
        "master_list": config_dir / "master_tracks_list.json",
    }


class TestWorkshopRootResolution:
    """Regression test for evidence 3 (workshop-ingest-hardening.md §3.1).

    ``workshop_content_root()`` used to be called ARGLESS at the top of the function,
    before lobby_config.json was even loaded, so its game_dir-derived candidate could
    never fire. In the container the install lives under /steam/... while every remaining
    candidate is /home/<user>/..., and the bot scanned **0** workshop tracks.
    """

    def test_a_workshop_track_under_the_config_derived_steamapps_is_scanned(self, fake_install):
        item = fake_install["steamapps"] / "workshop" / "content" / "410340" / "3141592653"
        item.parent.mkdir(parents=True)
        shutil.copytree(os.path.join(TRACKCHECK_FIXTURES, "split_pair_track"), str(item))

        gather_tracks_and_races()

        master = json.loads(fake_install["master_list"].read_text())
        assert "Fixture Split Track" in master["The Drawing Board"]["workshop"]

    def test_no_workshop_root_warns_and_still_writes_a_master_list(self, fake_install, capsys):
        gather_tracks_and_races()

        out = capsys.readouterr().out
        assert "no workshop content root found" in out
        assert str(fake_install["game_dir"]) in out
        assert fake_install["master_list"].exists()
