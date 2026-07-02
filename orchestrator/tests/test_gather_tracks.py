import pytest

from gather_tracks import is_tutorial, normalize_env


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
