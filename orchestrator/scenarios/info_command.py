"""Scenario: a client sends the /info chat command and the server bot responds with
the expected [INFO] message. Non-admin, non-mutating — safe even against a scratch
instance. See docs/features/doing/automated-testing.md Phase 3.

Contract each scenario module implements:
  CLIENT_NICKNAME  (optional, str)   -- nickname for the anonymous client instance
  CLIENT_SCRIPT    (list[str])       -- client_script.txt lines: "<delaySeconds> <message>"
  check(server_log_path, lobby_name) -- polls the SERVER's log (not the client's -- the
                                         client runs the same DLL and will also locally
                                         echo/respond to its own message, but only the
                                         server's own chat_response event is the real
                                         answer) and returns (passed: bool, reason: str)
"""
import scenario_harness as harness

CLIENT_NICKNAME = "ScenarioClient"
CLIENT_SCRIPT = ["5 /info"]
ASSERTION_TIMEOUT_S = 30


def check(server_log_path, lobby_name):
    # /info sends two chat_response events: a playlist/interval summary, then a
    # room-status one containing "Room: <name>". The room-status line is the more
    # specific assertion -- it proves the server actually processed /info *and* reports
    # the room we configured it to create, not just that some [INFO] text appeared.
    line = harness.wait_for_log_pattern(
        server_log_path,
        r'"event":"chat_response".*\[INFO\].*Room:',
        ASSERTION_TIMEOUT_S,
    )
    if line is None:
        return False, (
            f"Timed out after {ASSERTION_TIMEOUT_S}s waiting for the server's [INFO] "
            f"room-status chat_response in {server_log_path}."
        )
    if lobby_name not in line:
        return False, f"Got an [INFO] room-status response but it didn't mention the test lobby name '{lobby_name}': {line}"
    return True, f"Server responded to /info with the expected room-status message: {line}"
