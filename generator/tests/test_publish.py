"""Unit tests for the reworked generator/src/publish.py.

Never invokes real steamcmd/Steam: the process-launching seam (_invoke_steamcmd)
is always replaced by a fake `runner` callable that returns a canned stdout
fixture (loaded from generator/tests/fixtures/), so these tests exercise the
"grep the output, don't trust the exit code alone" parsing/verification logic
against realistic steamcmd output without any network/credential dependency.
"""

import os

import pytest

from src.publish import (
    PublishError,
    STEAMCMD_LOGIN_ACCOUNT,
    WORKSHOP_VISIBILITY_PUBLIC,
    _stdout_shows_failure,
    build_steamcmd_command,
    extract_new_workshop_id,
    publish_track,
    run_steamcmd,
    write_vdf,
)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), "r") as f:
        return f.read()


SUCCESS_NEW_ITEM = _load_fixture("steamcmd_success_new_item.txt")
SUCCESS_FALLBACK_FORMAT = _load_fixture("steamcmd_success_fallback_format.txt")
SUCCESS_UPDATE_EXISTING = _load_fixture("steamcmd_success_update_existing.txt")
SUCCESS_NO_PARSEABLE_ID = _load_fixture("steamcmd_success_no_parseable_id.txt")
FAILURE_LOGIN = _load_fixture("steamcmd_failure_login.txt")
FAILURE_UPLOAD_REJECTED = _load_fixture("steamcmd_failure_upload_rejected.txt")


class TestExtractNewWorkshopId:
    def test_parses_primary_created_new_item_format(self):
        assert extract_new_workshop_id(SUCCESS_NEW_ITEM) == "1234567890"

    def test_parses_fallback_id_format(self):
        assert extract_new_workshop_id(SUCCESS_FALLBACK_FORMAT) == "9988776655"

    def test_raises_publish_error_when_no_id_present(self):
        with pytest.raises(PublishError):
            extract_new_workshop_id(SUCCESS_NO_PARSEABLE_ID)


class TestStdoutShowsFailure:
    def test_success_output_is_not_a_failure(self):
        assert _stdout_shows_failure(SUCCESS_NEW_ITEM) is False

    def test_login_failure_is_detected(self):
        assert _stdout_shows_failure(FAILURE_LOGIN) is True

    def test_error_marker_is_detected(self):
        assert _stdout_shows_failure(FAILURE_UPLOAD_REJECTED) is True

    def test_update_success_output_is_not_a_failure(self):
        assert _stdout_shows_failure(SUCCESS_UPDATE_EXISTING) is False


class TestBuildSteamcmdCommand:
    def test_uses_configured_login_account(self):
        cmd = build_steamcmd_command("/tmp/whatever.vdf")
        assert STEAMCMD_LOGIN_ACCOUNT in cmd
        assert "/tmp/whatever.vdf" in cmd
        assert "+workshop_build_item" in cmd


class TestWriteVdf:
    def test_writes_public_visibility_by_default(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        write_vdf("0", "proc_batch_1", vdf_path=str(vdf_path))
        content = vdf_path.read_text()
        assert f'"visibility" "{WORKSHOP_VISIBILITY_PUBLIC}"' in content
        assert WORKSHOP_VISIBILITY_PUBLIC == "0"  # the adopted decision: public, not unlisted

    def test_writes_given_published_file_id(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        write_vdf("42", "proc_batch_1", vdf_path=str(vdf_path))
        assert '"publishedfileid" "42"' in vdf_path.read_text()

    def test_new_item_uses_zero_id(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        write_vdf("0", "proc_batch_1", vdf_path=str(vdf_path))
        assert '"publishedfileid" "0"' in vdf_path.read_text()

    def test_title_and_description_overridable(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        write_vdf("0", "proc_batch_1", title="Custom Title", description="Custom Desc", vdf_path=str(vdf_path))
        content = vdf_path.read_text()
        assert '"title" "Custom Title"' in content
        assert '"description" "Custom Desc"' in content

    def test_default_title_derived_from_track_id(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        write_vdf("0", "proc_batch_1", vdf_path=str(vdf_path))
        assert '"title" "Procedural FPV - Proc Batch 1"' in vdf_path.read_text()


class TestRunSteamcmd:
    def test_returns_runner_stdout(self):
        stdout = run_steamcmd("/tmp/x.vdf", runner=lambda cmd: SUCCESS_NEW_ITEM)
        assert stdout == SUCCESS_NEW_ITEM

    def test_passes_vdf_path_into_command(self):
        seen = {}

        def fake_runner(cmd):
            seen["cmd"] = cmd
            return SUCCESS_NEW_ITEM

        run_steamcmd("/tmp/x.vdf", runner=fake_runner)
        assert "/tmp/x.vdf" in seen["cmd"]

    def test_reraises_runner_exceptions(self):
        def failing_runner(cmd):
            raise RuntimeError("SteamCMD failed with exit code: 1")

        with pytest.raises(RuntimeError):
            run_steamcmd("/tmp/x.vdf", runner=failing_runner)


class TestPublishTrackNewItem:
    def _fake_stage(self, calls):
        def stage(track_id):
            calls.append(track_id)
        return stage

    def test_new_item_returns_parsed_id(self, tmp_path):
        stage_calls = []
        vdf_path = tmp_path / "build.vdf"
        result = publish_track(
            "proc_batch_1",
            runner=lambda cmd: SUCCESS_NEW_ITEM,
            stage_fn=self._fake_stage(stage_calls),
            vdf_path=str(vdf_path),
        )
        assert result == "1234567890"
        assert stage_calls == ["proc_batch_1"]

    def test_new_item_defaults_published_file_id_to_zero(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        publish_track(
            "proc_batch_1",
            runner=lambda cmd: SUCCESS_NEW_ITEM,
            stage_fn=self._fake_stage([]),
            vdf_path=str(vdf_path),
        )
        assert '"publishedfileid" "0"' in vdf_path.read_text()

    def test_new_item_raises_when_id_unparseable_even_on_exit_zero(self, tmp_path):
        # This is the "don't trust the exit code alone" case: the fake runner
        # here represents a process that exited 0 (no RuntimeError raised) but
        # whose output never actually confirms a created item.
        vdf_path = tmp_path / "build.vdf"
        with pytest.raises(PublishError):
            publish_track(
                "proc_batch_1",
                runner=lambda cmd: SUCCESS_NO_PARSEABLE_ID,
                stage_fn=self._fake_stage([]),
                vdf_path=str(vdf_path),
            )

    def test_new_item_raises_on_failure_output_even_without_runner_exception(self, tmp_path):
        # Simulates steamcmd's login failing but the wrapping process still
        # exiting 0 (no exception raised by the runner) -- the output-grep
        # must still catch this.
        vdf_path = tmp_path / "build.vdf"
        with pytest.raises(PublishError):
            publish_track(
                "proc_batch_1",
                runner=lambda cmd: FAILURE_LOGIN,
                stage_fn=self._fake_stage([]),
                vdf_path=str(vdf_path),
            )

    def test_propagates_real_runner_exceptions(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"

        def failing_runner(cmd):
            raise RuntimeError("SteamCMD failed with exit code: 1")

        with pytest.raises(RuntimeError):
            publish_track(
                "proc_batch_1",
                runner=failing_runner,
                stage_fn=self._fake_stage([]),
                vdf_path=str(vdf_path),
            )


class TestPublishTrackUpdateInPlace:
    def test_update_in_place_returns_same_id(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        result = publish_track(
            "proc_batch_1",
            published_file_id="3751155174",
            runner=lambda cmd: SUCCESS_UPDATE_EXISTING,
            stage_fn=lambda track_id: None,
            vdf_path=str(vdf_path),
        )
        assert result == "3751155174"
        assert '"publishedfileid" "3751155174"' in vdf_path.read_text()

    def test_update_in_place_does_not_require_a_parseable_new_id(self, tmp_path):
        # The update-in-place output format never contains "Created new item
        # with ID ..." -- publish_track must not require that pattern when
        # published_file_id is already known.
        vdf_path = tmp_path / "build.vdf"
        result = publish_track(
            "proc_batch_1",
            published_file_id="3751155174",
            runner=lambda cmd: SUCCESS_UPDATE_EXISTING,
            stage_fn=lambda track_id: None,
            vdf_path=str(vdf_path),
        )
        assert result == "3751155174"

    def test_update_in_place_still_checks_for_failure_markers(self, tmp_path):
        vdf_path = tmp_path / "build.vdf"
        with pytest.raises(PublishError):
            publish_track(
                "proc_batch_1",
                published_file_id="3751155174",
                runner=lambda cmd: FAILURE_UPLOAD_REJECTED,
                stage_fn=lambda track_id: None,
                vdf_path=str(vdf_path),
            )
