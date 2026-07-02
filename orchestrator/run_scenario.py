#!/usr/bin/env python3
"""CLI entry point for the black-box scenario/log-assertion test harness.

Usage: python3 orchestrator/run_scenario.py <scenario_name>

Stops production, runs the named scenario in full isolation (a scratch server-bot
instance on fpv_bot's real Liftoff Pro account + one anonymous client instance), and
asserts on the server's structured log output. Restores production to its exact
pre-run state (DLL + config files) and restarts it, no matter how the run ends.

See docs/features/doing/automated-testing.md Phase 3.
"""
import argparse
import importlib
import sys
import uuid

import scenario_harness as harness

# Without this, Python's stdout is block-buffered when redirected to a file (e.g. a
# background task's output capture), so print() calls interleave unpredictably with
# subprocess output that writes directly to the same fd -- confusing when debugging a
# failed run. Line-buffer explicitly so the printed order matches real time order.
sys.stdout.reconfigure(line_buffering=True)

SERVER_JOIN_TIMEOUT_S = 180
CLIENT_JOIN_TIMEOUT_S = 180

CONFIG_FILES_TOUCHED = (
    "lobby_name.txt", "rotation_interval.txt", "room_private.txt",
    "bot_nickname.txt", "use_liftoff_pro.txt", "client_script.txt",
)


def main():
    parser = argparse.ArgumentParser(description="Run a black-box bot scenario test.")
    parser.add_argument("scenario", help="Scenario module name under orchestrator/scenarios/ (e.g. info_command)")
    args = parser.parse_args()

    try:
        scenario = importlib.import_module(f"scenarios.{args.scenario}")
    except ImportError as e:
        print(f"FAIL: unknown scenario '{args.scenario}': {e}")
        sys.exit(1)

    lobby_name = f"ScenarioTest-{uuid.uuid4().hex[:6]}"
    server_log = harness.scenario_log_path("server")
    client_log = harness.scenario_log_path("client")

    server_proc = None
    client_proc = None
    passed = False
    reason = "Did not reach the assertion step."
    dll_backup = None
    orchestrator_script_backup = None

    try:
        dll_backup = harness.backup_dll()
        harness.deploy_dll_verified()
        orchestrator_script_backup = harness.backup_orchestrator_script()
        harness.deploy_orchestrator_script()
        harness.stop_all()
        harness.ensure_steam_running()

        with harness.BackedUpConfig(*CONFIG_FILES_TOUCHED):
            server_proc = harness.launch_server(lobby_name, server_log)
            server_line = harness.wait_for_log_pattern(server_log, r'"event":"room_entered"', SERVER_JOIN_TIMEOUT_S)
            if server_line is None:
                reason = f"Server bot never reached the room within {SERVER_JOIN_TIMEOUT_S}s (check {server_log})."
            else:
                print(f"[Scenario] Server entered room: {server_line}")
                client_nickname = getattr(scenario, "CLIENT_NICKNAME", "ScenarioClient")
                client_proc = harness.launch_client(client_nickname, client_log, scenario.CLIENT_SCRIPT)
                client_line = harness.wait_for_log_pattern(client_log, r'"event":"room_entered"', CLIENT_JOIN_TIMEOUT_S)
                if client_line is None:
                    reason = f"Client never reached the room within {CLIENT_JOIN_TIMEOUT_S}s (check {client_log})."
                else:
                    print(f"[Scenario] Client entered room: {client_line}")
                    passed, reason = scenario.check(server_log, lobby_name)

            harness.kill_process(client_proc)
            harness.kill_process(server_proc)
    finally:
        harness.stop_all()
        harness.restore_dll(dll_backup)
        harness.restore_orchestrator_script(orchestrator_script_backup)
        harness.restart_production()

    status = "PASS" if passed else "FAIL"
    print(f"\n{status}: {reason}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
