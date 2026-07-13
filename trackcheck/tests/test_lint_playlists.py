import json
import os
import subprocess
import sys

from trackcheck.lint_playlists import lint_playlists, main

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class TestLintPlaylistsFunction:
    def test_clean_playlists_have_no_findings(self):
        playlists = load_fixture("playlists_clean.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        assert findings == []

    def test_typo_track_name_is_flagged(self):
        playlists = load_fixture("playlists_typo.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        codes_for_typo = [f["code"] for f in findings if f["playlist"] == "typo_track"]
        assert "ENTRY_NO_MATCHES" in codes_for_typo
        # A single-entry playlist that matches nothing also has zero total resolution.
        assert "EMPTY_RESOLUTION" in codes_for_typo

    def test_unknown_environment_is_flagged(self):
        playlists = load_fixture("playlists_typo.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        codes = [f["code"] for f in findings if f["playlist"] == "typo_environment"]
        assert "UNKNOWN_ENVIRONMENT" in codes

    def test_unknown_mode_is_flagged(self):
        playlists = load_fixture("playlists_typo.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        codes = [f["code"] for f in findings if f["playlist"] == "unknown_mode"]
        assert "UNKNOWN_MODE" in codes

    def test_duplicate_entry_is_flagged(self):
        playlists = load_fixture("playlists_typo.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        codes = [f["code"] for f in findings if f["playlist"] == "duplicate_entry"]
        assert "DUPLICATE_ENTRY" in codes

    def test_empty_resolution_is_flagged(self):
        playlists = load_fixture("playlists_typo.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        codes = [f["code"] for f in findings if f["playlist"] == "empty_resolution"]
        assert "EMPTY_RESOLUTION" in codes

    def test_clean_control_playlist_in_typo_fixture_has_no_findings(self):
        # Regression guard: a clean playlist sitting alongside broken ones in the
        # same file must not pick up any cross-talk findings.
        playlists = load_fixture("playlists_typo.json")
        master = load_fixture("master_for_lint.json")
        findings = lint_playlists(playlists, master)
        codes = [f["code"] for f in findings if f["playlist"] == "clean_control"]
        assert codes == []

    def test_invalid_playlist_shape_is_flagged(self):
        findings = lint_playlists({"broken": "not-a-list"}, load_fixture("master_for_lint.json"))
        assert findings == [{
            "playlist": "broken", "code": "INVALID_PLAYLIST_SHAPE",
            "detail": "playlist value must be a list, got str",
        }]


class TestLintPlaylistsMain:
    def test_main_exits_zero_on_clean_fixtures(self):
        rc = main([
            "--playlists", os.path.join(FIXTURES, "playlists_clean.json"),
            "--master", os.path.join(FIXTURES, "master_for_lint.json"),
        ])
        assert rc == 0

    def test_main_exits_nonzero_on_typo_fixtures(self):
        rc = main([
            "--playlists", os.path.join(FIXTURES, "playlists_typo.json"),
            "--master", os.path.join(FIXTURES, "master_for_lint.json"),
        ])
        assert rc == 1

    def test_main_exits_nonzero_when_files_missing(self):
        rc = main(["--playlists", "/nonexistent/playlists.json", "--master", "/nonexistent/master.json"])
        assert rc == 2

    def test_cli_invocation_as_subprocess_exits_nonzero_on_typo(self):
        # Exercises the actual `python3 -m trackcheck.lint_playlists` entry point
        # (acceptance criterion: "Lint CLI flags a playlist entry with a typo'd
        # track name; exits nonzero"), not just the importable main() function.
        result = subprocess.run(
            [
                sys.executable, "-m", "trackcheck.lint_playlists",
                "--playlists", os.path.join(FIXTURES, "playlists_typo.json"),
                "--master", os.path.join(FIXTURES, "master_for_lint.json"),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "ENTRY_NO_MATCHES" in result.stdout
