"""Scenario: prove the command-registry refactor (Phase 2 A2) dispatches and gates
commands correctly, end-to-end, via the new CommandRegistry.Process path.

A non-admin anonymous client sends /info then /help. Against the SERVER's log we assert:

  1. ROUTING — /info still routes to InfoCommand and produces the [INFO] room-status
     chat_response (the registry replaced the old switch; this proves dispatch works).
  2. /help ENUMERATION — /help produces a themed [HELP] header (proves HelpCommand runs
     and paginates).
  3. PERMISSION FILTER (visible) — the non-admin /help lists /info (a public command).
  4. PERMISSION FILTER (hidden) — the non-admin /help does NOT list /kick (admin-only),
     proving CanExecute filters the help view. This is the single most important new
     behavior of the refactor: one permission predicate drives both visibility and
     execution.

Non-mutating: /info and /help have no side effects, so this is safe against a scratch
instance. Admin *success* paths (each command's Execute body, moved verbatim) are not
covered here — the anonymous client cannot be an admin — and remain a human check.

Help line format (from HelpCommand): FormatHighlight(Name) + " - " + Description, i.e.
`<b><color=#00FFFF>/info</color></b> - ...`; header via FormatTag("HELP", infoTagColor),
i.e. `<b><color=#0000FF>[HELP]</color></b> Commands (Page 1/...)`. JsonEscapeStrict passes
angle brackets/'#'/'[' through verbatim, so these appear literally in the JSONL line.

See docs/features/doing/chat-commands-refactor-help.md and automated-testing.md Phase 3.
"""
import scenario_harness as harness

CLIENT_NICKNAME = "SweepProbe"
CLIENT_SCRIPT = ["5 /info", "12 /help"]

ROUTE_TIMEOUT_S = 30
HELP_TIMEOUT_S = 30
ENTRY_TIMEOUT_S = 15
LEAK_TIMEOUT_S = 10

INFO_ROUTE_PATTERN = r'"event":"chat_response".*\[INFO\].*Room:'
HELP_HEADER_PATTERN = r'"event":"chat_response".*\[HELP\]</color></b> Commands \(Page 1'
HELP_INFO_ENTRY_PATTERN = r'"event":"chat_response".*/info</color></b> - '
# An admin-only command must NOT appear in a non-admin's /help. /kick is unconditionally
# admin-only (unlike /skip, which could go public under democracy mode).
HELP_ADMIN_LEAK_PATTERN = r'"event":"chat_response".*/kick</color></b> - '


def check(server_log_path, lobby_name):
    # 1. Routing: /info dispatches through the registry to InfoCommand.
    line = harness.wait_for_log_pattern(server_log_path, INFO_ROUTE_PATTERN, ROUTE_TIMEOUT_S)
    if line is None:
        return False, (
            f"Timed out after {ROUTE_TIMEOUT_S}s waiting for the server's [INFO] room-status "
            f"chat_response — /info did not route through CommandRegistry.Process."
        )
    if lobby_name not in line:
        return False, f"/info routed but the response didn't mention the test lobby '{lobby_name}': {line}"

    # 2. /help runs and emits its themed header.
    header = harness.wait_for_log_pattern(server_log_path, HELP_HEADER_PATTERN, HELP_TIMEOUT_S)
    if header is None:
        return False, (
            f"Timed out after {HELP_TIMEOUT_S}s waiting for the [HELP] header chat_response — "
            f"HelpCommand did not run or did not format via the theme helpers."
        )

    # 3. Permission filter (visible): a public command (/info) is listed for the non-admin.
    entry = harness.wait_for_log_pattern(server_log_path, HELP_INFO_ENTRY_PATTERN, ENTRY_TIMEOUT_S)
    if entry is None:
        return False, (
            f"[HELP] header appeared but no '/info' entry was listed within {ENTRY_TIMEOUT_S}s — "
            f"help enumeration/pagination is wrong."
        )

    # 4. Permission filter (hidden): an admin-only command (/kick) must be absent from the
    #    non-admin's help. wait_for_log_pattern returning non-None here means a LEAK.
    leak = harness.wait_for_log_pattern(server_log_path, HELP_ADMIN_LEAK_PATTERN, LEAK_TIMEOUT_S)
    if leak is not None:
        return False, (
            f"SECURITY/REFACTOR REGRESSION: admin-only /kick was listed in a non-admin's /help "
            f"— CanExecute is not filtering the help view: {leak}"
        )

    return True, (
        "Command registry verified: /info routed to InfoCommand; /help ran, paginated, listed "
        "the public /info entry, and correctly hid admin-only /kick from the non-admin view."
    )
