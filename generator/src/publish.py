"""generator/src/publish.py -- stage a generated track and publish it to Steam
Workshop via steamcmd.

Reworked per docs/features/doing/procedural-gen-improvements.md ("Known gap" #1):
this now defaults to creating a brand-new workshop item per call
(publishedfileid "0"), parses the new item's ID out of steamcmd's OUTPUT text
(never trusting the exit code alone -- see PublishError / extract_new_workshop_id
/ _stdout_shows_failure), and keeps an explicit update-in-place path (pass an
existing published_file_id) for fixing an already-published item. The actual
steamcmd subprocess call is isolated behind a single seam, `_invoke_steamcmd`,
so the whole pipeline is unit-testable with a fake `runner` -- see
generator/tests/test_publish.py. Nothing in this module is ever invoked against
real steamcmd/Steam by the test suite.
"""

import os
import re
import shutil
import subprocess
import sys

PROJECT_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STAGING_DIR = os.path.join(PROJECT_WORKSPACE, "workshop_staging")
VDF_PATH = os.path.join(PROJECT_WORKSPACE, "workshop_build.vdf")
PREVIEW_PATH = os.path.join(PROJECT_WORKSPACE, "preview.jpg")

STEAMCMD_PATH = "/usr/games/steamcmd"
STEAM_APP_ID = "410340"  # Liftoff App ID

# --- Publish-account decision (see the feature doc, "Publish-account decision")
# ---
# The feature spec recommends publishing as dev-user's personal Steam account
# with public visibility (the bot then just downloads a public item by workshop
# ID like any other track -- no special auth needed on the bot side). The code
# as found here already logs into steamcmd as the BOT's own Steam account
# ("fpv_bot"), via an isolated $HOME (see _real_steamcmd_env) specifically so it
# can't disturb dev-user's live desktop Steam session -- a real, deliberate,
# already-working design, not a placeholder. That is a genuine conflict between
# the spec's recommendation and the code as found. Per AGENTS.md ("if the spec
# contradicts what you find in the code, stop and record the conflict instead
# of improvising") this is NOT silently changed: STEAMCMD_LOGIN_ACCOUNT stays
# "fpv_bot" (today's real, tested behavior) and the account choice is recorded
# as an OPEN decision for the human operator in the feature doc. Visibility,
# which is just a VDF text value and touches no credentials, HAS been updated
# to match the recommendation -- see WORKSHOP_VISIBILITY_PUBLIC below.
STEAMCMD_LOGIN_ACCOUNT = "fpv_bot"

# Steamworks ERemoteStoragePublishedFileVisibility: 0=Public, 1=FriendsOnly,
# 2=Private, 3=Unlisted (stable, documented Steamworks SDK constants -- not a
# guess). The code as found published "3" (Unlisted); the feature doc's adopted
# decision is public visibility, so new/updated items now write "0".
WORKSHOP_VISIBILITY_PUBLIC = "0"

# Known-good ID-parsing patterns for steamcmd's `+workshop_build_item` success
# output, carried over unchanged from the pre-rework code (these are what
# actually produced this project's one existing published item, so they are
# treated as field-verified rather than guessed -- see AGENTS.md rule 1).
_NEW_ITEM_ID_PATTERNS = (
    re.compile(r"Created new item with ID\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bID\s+(\d+)\b"),
)

# steamcmd's own well-documented convention: failure lines are prefixed
# "ERROR!", and a failed login line reads "...to Steam Public...FAILED (reason)".
# Used to catch "exit code 0 but nothing actually happened" cases, since the
# spec explicitly requires not trusting the exit code alone.
_FAILURE_MARKERS = ("ERROR!", "FAILED (")


class PublishError(Exception):
    """Raised when steamcmd's own output shows the publish did not actually
    succeed, even if the process exited 0 -- an exit code of 0 is necessary but
    not sufficient (see run_steamcmd / extract_new_workshop_id)."""


def load_workshop_config():
    """Deprecated: superseded by generator/src/registry.py (one registry row
    per published item instead of one global "current" published_file_id).
    Kept only as a compatibility no-op in case an old caller still imports it;
    new code should use registry.load_registry() instead."""
    return {"published_file_id": "0"}


def stage_files(track_id):
    """
    Clears the staging directory and copies both the .track and .race files into it.
    """
    # Create or clean staging dir
    if os.path.exists(STAGING_DIR):
        shutil.rmtree(STAGING_DIR)
    os.makedirs(STAGING_DIR, exist_ok=True)

    # Paths in Liftoff config
    liftoff_base = os.path.expanduser("~/.config/unity3d/LuGus Studios/Liftoff")
    track_dir = os.path.join(liftoff_base, "Tracks", track_id)

    # Locate race dir matching track_id prefix dynamically
    races_parent_dir = os.path.join(liftoff_base, "Races")
    race_dir = None
    if os.path.exists(races_parent_dir):
        matching_dirs = [
            os.path.join(races_parent_dir, d)
            for d in os.listdir(races_parent_dir)
            if d.startswith(f"{track_id}_race") and os.path.isdir(os.path.join(races_parent_dir, d))
        ]
        if matching_dirs:
            # Sort by modification time to pick the newest one
            matching_dirs.sort(key=os.path.getmtime)
            race_dir = matching_dirs[-1]

    if not race_dir:
        race_dir = os.path.join(races_parent_dir, f"{track_id}_race")

    # Locate files
    track_files = [f for f in os.listdir(track_dir) if f.endswith(".track")] if os.path.exists(track_dir) else []
    race_files = [f for f in os.listdir(race_dir) if f.endswith(".race")] if os.path.exists(race_dir) else []

    if not track_files:
        raise ValueError(f"No .track files found in {track_dir}")
    if not race_files:
        raise ValueError(f"No .race files found in {race_dir}")

    # Copy files directly to staging root
    for tf in track_files:
        shutil.copy2(os.path.join(track_dir, tf), os.path.join(STAGING_DIR, tf))
    for rf in race_files:
        shutil.copy2(os.path.join(race_dir, rf), os.path.join(STAGING_DIR, rf))

    print(f"[Publish] Staged files for track '{track_id}' from {track_dir} and race from {race_dir} in: {STAGING_DIR}")


def write_vdf(published_file_id, track_id, title=None, description=None, vdf_path=None):
    """
    Writes the Steam VDF file for workshop publishing. `published_file_id` "0"
    creates a new item; any other value updates that item in place.
    `vdf_path` defaults to the module-level VDF_PATH; tests pass a tmp path so
    they never touch the repo tree.
    """
    resolved_vdf_path = vdf_path or VDF_PATH

    # Clean up track_id to make it a presentable title (e.g. "proc_loop_1" -> "Proc Loop 1")
    title_suffix = track_id.replace("_", " ").title()
    resolved_title = title or f"Procedural FPV - {title_suffix}"
    resolved_description = description or "Procedurally generated FPV track & race loop by fpv_bot"

    vdf_content = f"""\
"workshopitem"
{{
  "appid" "{STEAM_APP_ID}"
  "publishedfileid" "{published_file_id}"
  "contentfolder" "{STAGING_DIR}"
  "previewfile" "{PREVIEW_PATH}"
  "title" "{resolved_title}"
  "description" "{resolved_description}"
  "changenote" "Automatic updates from generator"
  "visibility" "{WORKSHOP_VISIBILITY_PUBLIC}"
}}
"""
    with open(resolved_vdf_path, "w") as f:
        f.write(vdf_content)
    print(f"[Publish] Generated VDF file at: {resolved_vdf_path}")
    return resolved_vdf_path


def _real_steamcmd_env():
    """Builds the isolated environment for a REAL steamcmd invocation: a
    dedicated $HOME so steamcmd's own login cache can't conflict with (or log
    out) the interactive user's primary Steam client session. Only ever called
    by _invoke_steamcmd -- fake runners used in tests never reach this, so
    tests never touch real directories under generator/.steamcmd_home."""
    steamcmd_home = os.path.abspath(os.path.join(PROJECT_WORKSPACE, ".steamcmd_home"))
    os.makedirs(steamcmd_home, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = steamcmd_home
    env["STEAM_HOME"] = steamcmd_home
    env["XDG_DATA_HOME"] = os.path.join(steamcmd_home, ".local/share")
    env["XDG_CONFIG_HOME"] = os.path.join(steamcmd_home, ".config")
    env["XDG_CACHE_HOME"] = os.path.join(steamcmd_home, ".cache")
    return env


def build_steamcmd_command(vdf_path):
    """Pure command construction -- no side effects, freely testable."""
    return [
        STEAMCMD_PATH,
        "+@NoPromptForPassword", "1",
        "+login", STEAMCMD_LOGIN_ACCOUNT,
        "+workshop_build_item", vdf_path,
        "+quit",
    ]


def _invoke_steamcmd(cmd):
    """The ONLY function in this codebase that shells out to steamcmd for real.
    Everything else (run_steamcmd, publish_track) takes a `runner` callable
    that defaults to this function, so the entire pipeline is testable without
    ever invoking real steamcmd/Steam. Returns combined stdout+stderr text;
    raises RuntimeError if the process exits non-zero (the exit code IS still
    checked here -- it's just not *trusted alone* for success; see
    run_steamcmd's caller for the output-grep on top of this)."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_real_steamcmd_env(),
    )

    stdout_lines = []
    while True:
        line = process.stdout.readline()
        if not line:
            break
        sys.stdout.write(f"  [SteamCMD] {line}")
        sys.stdout.flush()
        stdout_lines.append(line)

    process.wait()
    stdout = "".join(stdout_lines)

    if process.returncode != 0:
        raise RuntimeError(f"SteamCMD failed with exit code: {process.returncode}")

    return stdout


def run_steamcmd(vdf_path, runner=None):
    """
    Runs steamcmd to upload/update the workshop item described by `vdf_path`.
    `runner(cmd) -> stdout` defaults to _invoke_steamcmd; pass a fake in tests.
    """
    runner = runner or _invoke_steamcmd
    cmd = build_steamcmd_command(vdf_path)

    print("[Publish] Initiating SteamCMD upload...")
    print(f"[Publish] Command: {' '.join(cmd)}")

    try:
        return runner(cmd)
    except Exception as e:
        print("\n" + "=" * 80)
        print("[Publish] ERROR: SteamCMD execution failed or returned an error.")
        print(f"Details: {e}")
        print("-" * 80)
        print("This is likely because SteamCMD needs you to log in manually to the isolated environment.")
        print(f"To log in and cache credentials for '{STEAMCMD_LOGIN_ACCOUNT}', run this command in your terminal:")
        steamcmd_home = os.path.abspath(os.path.join(PROJECT_WORKSPACE, ".steamcmd_home"))
        print(f"\n    HOME={steamcmd_home} {STEAMCMD_PATH} +login {STEAMCMD_LOGIN_ACCOUNT}\n")
        print("Enter your password and Steam Guard code when prompted, then type 'quit' to exit.")
        print("This only needs to be done once! Subsequent publishes will use the cached session.")
        print("=" * 80 + "\n")
        raise


def _stdout_shows_failure(stdout: str) -> bool:
    """steamcmd sometimes exits 0 even though the operation it was asked to do
    did not happen (e.g. a login failure inside a longer +@NoPromptForPassword
    session). Grepping for these well-documented failure markers is the "don't
    trust the exit code alone" check the feature doc calls for, applied to both
    the new-item and update-in-place paths."""
    return any(marker in stdout for marker in _FAILURE_MARKERS)


def extract_new_workshop_id(stdout: str) -> str:
    """Parse the newly created item's workshop ID out of steamcmd's output.
    Raises PublishError (not a silent None) if no ID can be found -- a missing
    ID after a "successful" (exit 0) run must never be reported as success."""
    for pattern in _NEW_ITEM_ID_PATTERNS:
        match = pattern.search(stdout)
        if match:
            return match.group(1)
    raise PublishError(
        "steamcmd exited successfully but no new workshop ID could be parsed "
        "from its output -- refusing to report success on exit code alone. "
        "Raw output follows:\n" + stdout
    )


def publish_track(track_id, published_file_id="0", *, runner=None, stage_fn=None, vdf_path=None,
                   title=None, description=None):
    """
    Full pipeline to stage and publish a track to Steam Workshop.

    published_file_id="0" (the default) creates a brand-new workshop item --
    this is the normal path for the batch pipeline, one item per track. Pass an
    existing item's ID to update it in place instead (the "fixing an existing
    item" path the feature doc asks to keep).

    Returns the resulting workshop ID as a string (the newly created one, or
    the same `published_file_id` that was updated). Raises PublishError if
    steamcmd's output does not confirm success -- callers must not treat "no
    exception" alone from a lower-level function as proof of success; this
    function is the one place that turns exit-code-0-but-actually-failed into
    an explicit error.

    `runner` / `stage_fn` are the two real-environment seams (steamcmd
    subprocess, Liftoff install filesystem scan) -- both injectable so this
    whole function is unit-testable. See generator/tests/test_publish.py.
    """
    stage = stage_fn or stage_files
    resolved_vdf_path = vdf_path or VDF_PATH

    # 1. Stage the files
    stage(track_id)

    # 2. Generate VDF ("0" = new item; anything else = update that item)
    write_vdf(published_file_id, track_id, title=title, description=description, vdf_path=resolved_vdf_path)

    # 3. Upload
    stdout = run_steamcmd(resolved_vdf_path, runner=runner)

    # 4. Verify -- never trust the exit code alone (see _stdout_shows_failure).
    if _stdout_shows_failure(stdout):
        raise PublishError(f"steamcmd exited 0 but its output reports a failure:\n{stdout}")

    # 5. Extract / confirm the published ID.
    if published_file_id == "0":
        new_id = extract_new_workshop_id(stdout)
        print(f"\n[Publish] SUCCESS! Created new Steam Workshop item with ID: {new_id}")
        print(f"[Publish] Workshop URL: https://steamcommunity.com/sharedfiles/filedetails/?id={new_id}")
        return new_id
    else:
        print(f"\n[Publish] SUCCESS! Updated existing Steam Workshop item {published_file_id}")
        return published_file_id


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Publish a generated track to Steam Workshop.")
    parser.add_argument("track_id", help="The track's local ID (folder name under Liftoff's Tracks/).")
    parser.add_argument(
        "--update", metavar="WORKSHOP_ID", default=None,
        help="Update an EXISTING workshop item in place instead of creating a new one.",
    )
    args = parser.parse_args()

    result_id = publish_track(args.track_id, published_file_id=(args.update or "0"))
    print(f"[Publish] Result workshop ID: {result_id}")
