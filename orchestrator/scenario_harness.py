"""Shared library for the black-box scenario/log-assertion test harness.

See docs/features/doing/automated-testing.md (Phase 3) and
docs/features/doing/multi-lobby-bot-scaling.md for the design rationale and the
hard-won gotchas this code works around (single shared Player.log that truncates on a
second launch, config files read once per-process at Awake(), the need to md5sum-verify
every deploy, etc).

Everything that touches the `fpv_bot` account goes through `sudo -u fpv_bot -n` — never
plain `sudo` (root), since the dev user's home is mode 750 and fpv_bot cannot traverse
into it directly. Anything that needs to move bytes from the dev user's filesystem into
fpv_bot's (the built DLL, config file contents) is read locally as the dev user first,
then piped into a `tee`/`cat` running as fpv_bot via stdin — never a cross-user `cp`.

The reverse also holds: `/home/fpv_bot` itself is mode 750, so the dev user cannot `cd`
into anything under it either (confirmed live — `cd /home/fpv_bot/...` fails with
"permission denied"). subprocess.Popen's `cwd=` performs the chdir in the
child *before* exec, i.e. still as the dev user, so it can never point under
`/home/fpv_bot`. Every launch below therefore uses absolute paths for everything and
never passes `cwd=` for a path under `/home/fpv_bot` — Python resolves `__file__`
absolutely regardless of cwd, and `run_bepinex.sh`/`Liftoff.x86_64` are invoked by
absolute path too, so no cwd is actually required.
"""
import hashlib
import os
import re
import subprocess
import time
import uuid

MAIN_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME_DIR = "/home/fpv_bot/.steam/debian-installation/steamapps/common/Liftoff"
PLUGINS_DIR = f"{GAME_DIR}/BepInEx/plugins"
SCENARIO_LOG_DIR = "/home/fpv_bot/scenario_logs"
KILL_BOT_SH = f"{MAIN_CHECKOUT}/scripts/kill_bot.sh"
RUN_BOT_SH = f"{MAIN_CHECKOUT}/scripts/run_bot.sh"
RUN_BEPINEX_SH = f"{GAME_DIR}/run_bepinex.sh"
LIFTOFF_EXE = f"{GAME_DIR}/Liftoff.x86_64"

# fpv_bot's own deployed copy of the project (set up by infra/setup_bot.sh). The server
# role must run *from here*, not from a checkout under the dev user's home -- that
# directory is mode 750, so fpv_bot can't even traverse into it, let alone read a
# script out of it (confirmed live: "python3: can't open file
# '/home/<dev>/.../run_headless_lobby.py': [Errno 13] Permission denied").
BOT_PROJECT_DIR = "/home/fpv_bot/procedural-fpv"
ORCHESTRATOR_SCRIPT_REMOTE = f"{BOT_PROJECT_DIR}/orchestrator/run_headless_lobby.py"
# run_headless_lobby.py imports event_log at module load; it must be deployed alongside
# or the orchestrator ImportErrors on start.
EVENT_LOG_MODULE_REMOTE = f"{BOT_PROJECT_DIR}/orchestrator/event_log.py"

THIS_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DLL_PATH = os.path.join(THIS_REPO_DIR, "plugin", "bin", "Debug", "LiftoffAutoLobby.dll")
LOCAL_ORCHESTRATOR_SCRIPT = os.path.join(THIS_REPO_DIR, "orchestrator", "run_headless_lobby.py")
LOCAL_EVENT_LOG_MODULE = os.path.join(THIS_REPO_DIR, "orchestrator", "event_log.py")
LOCAL_RUN_LOG_DIR = os.path.join(THIS_REPO_DIR, "orchestrator", "scenario_run_logs")


class HarnessError(Exception):
    pass


def _sudo_fpv(args, **kw):
    return subprocess.run(["sudo", "-u", "fpv_bot", "-n"] + args, **kw)


def plugin_path(name):
    return os.path.join(PLUGINS_DIR, name)


def read_fpv_binary(remote_path):
    res = _sudo_fpv(["cat", remote_path], capture_output=True)
    if res.returncode != 0:
        return None
    return res.stdout


def read_fpv_text(remote_path):
    data = read_fpv_binary(remote_path)
    return None if data is None else data.decode("utf-8", errors="replace")


def write_fpv_binary(remote_path, data: bytes):
    res = _sudo_fpv(["tee", remote_path], input=data, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise HarnessError(f"Failed to write {remote_path}: {res.stderr.decode(errors='replace')}")


def write_fpv_text(remote_path, text: str):
    write_fpv_binary(remote_path, text.encode("utf-8"))


def delete_fpv_file(remote_path):
    _sudo_fpv(["rm", "-f", remote_path])


class BackedUpConfig:
    """Context manager: snapshots the given BepInEx/plugins config files (content or
    absence) on entry, restores them exactly on exit regardless of what happens inside
    the block. Config files are read once per-process at Awake() (see
    multi-lobby-bot-scaling.md), so it's safe to overwrite them for a new launch — but
    they must be restored before production's *next* restart."""

    def __init__(self, *names):
        self.names = names
        self._snapshots = {}

    def __enter__(self):
        for name in self.names:
            self._snapshots[name] = read_fpv_binary(plugin_path(name))  # None if absent
        return self

    def __exit__(self, exc_type, exc, tb):
        for name in self.names:
            original = self._snapshots.get(name)
            path = plugin_path(name)
            if original is None:
                delete_fpv_file(path)
            else:
                write_fpv_binary(path, original)
        return False


def local_md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def remote_dll_md5():
    data = read_fpv_binary(plugin_path("LiftoffAutoLobby.dll"))
    return None if data is None else hashlib.md5(data).hexdigest()


def backup_dll():
    """Bytes of whatever's currently deployed (or None if absent) — snapshot before
    touching anything, so production can be restored to its exact pre-test binary."""
    return read_fpv_binary(plugin_path("LiftoffAutoLobby.dll"))


def restore_dll(backup_bytes):
    path = plugin_path("LiftoffAutoLobby.dll")
    if backup_bytes is None:
        delete_fpv_file(path)
    else:
        write_fpv_binary(path, backup_bytes)


def backup_orchestrator_script():
    """Bytes of whatever's currently at fpv_bot's own run_headless_lobby.py (or None if
    absent) -- production restarts from this exact path, so it must come back on its
    pre-test content (main's version), not this feature branch's, once testing is done."""
    return read_fpv_binary(ORCHESTRATOR_SCRIPT_REMOTE)


def restore_orchestrator_script(backup_bytes):
    if backup_bytes is None:
        delete_fpv_file(ORCHESTRATOR_SCRIPT_REMOTE)
    else:
        write_fpv_binary(ORCHESTRATOR_SCRIPT_REMOTE, backup_bytes)


def deploy_orchestrator_script():
    """Deploys this worktree's run_headless_lobby.py (with --log-file support) to
    fpv_bot's project copy so the 'server bot' launch can actually use it. Only the
    orchestrator's own modules and the control-plane package are touched -- gather_tracks.py
    and the JSON configs already there (from infra/setup_bot.sh) are untouched and
    unmodified by this branch."""
    print("[Harness] Deploying orchestrator script (run_headless_lobby.py + event_log.py "
          "+ dashboard/control/) for the test run...")
    with open(LOCAL_ORCHESTRATOR_SCRIPT, "rb") as f:
        write_fpv_binary(ORCHESTRATOR_SCRIPT_REMOTE, f.read())
    with open(LOCAL_EVENT_LOG_MODULE, "rb") as f:
        write_fpv_binary(EVENT_LOG_MODULE_REMOTE, f.read())
    deploy_control_package()


def deploy_control_package():
    """Deploy dashboard/control/ (plus dashboard/__init__.py) alongside the orchestrator.

    run_headless_lobby.py imports the control plane at module load since bot-dashboard.md's
    D5 extraction, so deploying the script without this package ImportErrors on start --
    exactly the failure mode the event_log.py deploy above already exists to prevent.
    Directories are created as fpv_bot first: `tee` cannot create a missing parent."""
    for rel_dir in ("dashboard", "dashboard/control"):
        res = _sudo_fpv(["mkdir", "-p", f"{BOT_PROJECT_DIR}/{rel_dir}"], stderr=subprocess.PIPE)
        if res.returncode != 0:
            raise HarnessError(f"Failed to create {rel_dir} in fpv_bot's project copy: "
                               f"{res.stderr.decode(errors='replace')}")

    local_pkg = os.path.join(THIS_REPO_DIR, "dashboard")
    rel_paths = ["__init__.py"] + [
        os.path.join("control", name)
        for name in sorted(os.listdir(os.path.join(local_pkg, "control")))
        if name.endswith(".py")
    ]
    for rel_path in rel_paths:
        with open(os.path.join(local_pkg, rel_path), "rb") as f:
            write_fpv_binary(f"{BOT_PROJECT_DIR}/dashboard/{rel_path}", f.read())


def build_plugin():
    print("[Harness] Building plugin...")
    res = subprocess.run(
        ["dotnet", "build", os.path.join(THIS_REPO_DIR, "plugin"), "-c", "Debug"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise HarnessError(f"Plugin build failed:\n{res.stdout}\n{res.stderr}")


def deploy_dll_verified():
    """Builds this worktree's plugin, deploys to fpv_bot only if the built DLL differs
    from what's already deployed, and always verifies byte-identity via md5 afterward.
    Never trust a deploy without checking — see multi-lobby-bot-scaling.md: "we got
    burned once by a stale deploy from unrelated concurrent work"."""
    build_plugin()
    want_md5 = local_md5(LOCAL_DLL_PATH)
    have_md5 = remote_dll_md5()
    if want_md5 != have_md5:
        print(f"[Harness] Deploying DLL ({have_md5} -> {want_md5})...")
        with open(LOCAL_DLL_PATH, "rb") as f:
            write_fpv_binary(plugin_path("LiftoffAutoLobby.dll"), f.read())
    verify_md5 = remote_dll_md5()
    if verify_md5 != want_md5:
        raise HarnessError(f"DLL deploy verification failed: expected {want_md5}, got {verify_md5}")
    print(f"[Harness] Deployed DLL verified: {want_md5}")


def stop_all():
    print("[Harness] Stopping all fpv_bot bot/Liftoff/Steam processes (kill_bot.sh)...")
    subprocess.run(["bash", KILL_BOT_SH], check=True)


def ensure_steam_running():
    """kill_bot.sh (stop_all) kills fpv_bot's Steam client along with everything else.
    Liftoff needs a live local Steam client for the Steamworks API to initialize (even
    with the SteamAppId bypass) -- confirmed live: launching Liftoff right after
    stop_all() without this produced a broken instance. Mirrors run_bot.sh's own
    xhost-grant + Steam-check-and-start-if-needed logic exactly."""
    subprocess.run(["xhost", "+SI:localuser:fpv_bot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = _sudo_fpv(["pgrep", "-u", "fpv_bot", "-x", "steam"], capture_output=True)
    if res.returncode == 0:
        print("[Harness] Steam already running for fpv_bot.")
        return
    print("[Harness] Starting Steam for fpv_bot...")
    subprocess.Popen(
        ["sudo", "-u", "fpv_bot", "-n", "-H", "env", "DISPLAY=:0", "XDG_RUNTIME_DIR=/run/user/1003",
         "dbus-run-session", "/usr/games/steam"],
        stdout=_local_run_log("steam_stdout"), stderr=subprocess.STDOUT,
    )
    print("[Harness] Waiting 15s for Steam to initialize...")
    time.sleep(15)


def restart_production():
    """Always restores production from the original main checkout's run_bot.sh
    (--no-build), never the feature-branch worktree — production must come back on
    main's code, not whatever untested branch the harness happened to run from."""
    print("[Harness] Restarting production (main checkout, --no-build)...")
    os.makedirs(LOCAL_RUN_LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOCAL_RUN_LOG_DIR, "production_restart.log")
    with open(log_path, "wb") as log_f:
        subprocess.Popen(
            ["bash", RUN_BOT_SH, "--no-build"],
            cwd=MAIN_CHECKOUT, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True,
        )


def ensure_log_dir():
    _sudo_fpv(["mkdir", "-p", SCENARIO_LOG_DIR])


def scenario_log_path(role):
    return f"{SCENARIO_LOG_DIR}/{role}_{uuid.uuid4().hex[:8]}.log"


def _local_run_log(name):
    os.makedirs(LOCAL_RUN_LOG_DIR, exist_ok=True)
    return open(os.path.join(LOCAL_RUN_LOG_DIR, f"{name}.log"), "wb")


def launch_server(lobby_name, log_path, interval=3600, playlist="all_official_races", width=640, height=480):
    """Runs run_headless_lobby.py from fpv_bot's own deployed project copy (see
    deploy_orchestrator_script -- fpv_bot can't read this worktree directly, matching
    exactly how run_bot.sh itself invokes production) as the 'server bot' identity
    (Liftoff Pro account, since use_liftoff_pro.txt is left untouched/absent = default
    true)."""
    ensure_log_dir()
    delete_fpv_file(log_path)
    cmd = [
        "sudo", "-u", "fpv_bot", "-n", "-H", "env", "DISPLAY=:0", "XDG_RUNTIME_DIR=/run/user/1003",
        "python3", ORCHESTRATOR_SCRIPT_REMOTE,
        "--playlist", playlist, "--interval", str(interval), "--lobby-name", lobby_name,
        "--log-file", log_path, "--gui", "--width", str(width), "--height", str(height),
    ]
    print(f"[Harness] Launching server bot (lobby='{lobby_name}', log={log_path})...")
    return subprocess.Popen(cmd, stdout=_local_run_log("server_stdout"), stderr=subprocess.STDOUT)


def launch_client(nickname, log_path, script_lines, width=640, height=480):
    """Launches Liftoff directly (not via the orchestrator monitor loop — a client is a
    throwaway single-shot instance) with anonymous sign-in and a client_script.txt, the
    exact manual invocation already proven in multi-lobby-bot-scaling.md."""
    ensure_log_dir()
    delete_fpv_file(log_path)
    write_fpv_text(plugin_path("use_liftoff_pro.txt"), "false")
    write_fpv_text(plugin_path("bot_nickname.txt"), nickname)
    write_fpv_text(plugin_path("client_script.txt"), "\n".join(script_lines) + "\n")
    cmd = [
        "sudo", "-u", "fpv_bot", "-n", "-H", "env", "DISPLAY=:0", "XDG_RUNTIME_DIR=/run/user/1003",
        "SteamAppId=410340", "STEAM_APP_ID=410340",
        RUN_BEPINEX_SH, LIFTOFF_EXE,
        "-logFile", log_path,
        "-screen-width", str(width), "-screen-height", str(height), "-screen-fullscreen", "0",
    ]
    print(f"[Harness] Launching anonymous client (nickname='{nickname}', log={log_path})...")
    return subprocess.Popen(cmd, stdout=_local_run_log("client_stdout"), stderr=subprocess.STDOUT)


def wait_for_log_pattern(log_path, pattern, timeout_s, poll=1.0):
    """Polls log_path (read as fpv_bot) for a line matching `pattern` (regex, searched
    per-line). Returns the matched line, or None on timeout. Tolerant of the file not
    existing yet (fresh launch)."""
    regex = re.compile(pattern)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        text = read_fpv_text(log_path)
        if text:
            for line in text.splitlines():
                if regex.search(line):
                    return line
        time.sleep(poll)
    return None


def kill_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
