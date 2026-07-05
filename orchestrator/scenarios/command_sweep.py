"""Scenario: prove the command-registry refactor (Phase 2 A2) dispatches and gates
commands correctly, end-to-end, via the new CommandRegistry.Process path.

A non-admin anonymous client sends /info then /help. Against the SERVER's log we assert:

  1. ROUTING — /info still routes to InfoCommand (the registry replaced the old switch;
     this proves dispatch works). Since chat-commands-v2, /info emits TWO lines via the
     SendTaggedLines multi-line helper: line 1 `[INFO] … Current: env - track` (tag +
     current-track label) and an UNTAGGED continuation `<…#888888…> ↳ Room: <lobby> …`.
     We assert both: the tagged first line carries `[INFO]` + `Current:`, and the room
     info moved to a continuation line that carries the muted marker color (#888888) and
     the lobby name but NOT `[INFO]` — proving the "tag appears once" behavior.
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

# Since chat-commands-v2, /info's tagged first line carries [INFO] + the "Current:" track
# label; the room-status moved to an untagged ↳ continuation line (see INFO_ROOM_CONT_PATTERN).
INFO_ROUTE_PATTERN = r'"event":"chat_response".*\[INFO\].*Current:'
# The continuation room line: muted marker color (#888888, only on continuations) + Room:,
# and — proving "tag appears once" — it must NOT contain [INFO] (asserted in check()).
INFO_ROOM_CONT_PATTERN = r'"event":"chat_response".*#888888.*Room:'
HELP_HEADER_PATTERN = r'"event":"chat_response".*\[HELP\]</color></b> Commands \(Page 1'
HELP_INFO_ENTRY_PATTERN = r'"event":"chat_response".*/info</color></b> - '
# An admin-only command must NOT appear in a non-admin's /help. /kick is unconditionally
# admin-only (unlike /skip, which could go public under democracy mode).
HELP_ADMIN_LEAK_PATTERN = r'"event":"chat_response".*/kick</color></b> - '


def check(server_log_path, lobby_name):
    # 1a. Routing: /info dispatches through the registry to InfoCommand and emits its
    #     tagged first line ([INFO] + the new "Current:" track label).
    line = harness.wait_for_log_pattern(server_log_path, INFO_ROUTE_PATTERN, ROUTE_TIMEOUT_S)
    if line is None:
        return False, (
            f"Timed out after {ROUTE_TIMEOUT_S}s waiting for the server's tagged [INFO] "
            f"'Current:' chat_response — /info did not route through CommandRegistry.Process."
        )

    # 1b. Multi-line "tag once": the room status moved to an untagged ↳ continuation line
    #     that carries the muted marker color and the lobby name but NOT the [INFO] tag.
    room = harness.wait_for_log_pattern(server_log_path, INFO_ROOM_CONT_PATTERN, ROUTE_TIMEOUT_S)
    if room is None:
        return False, (
            f"Timed out after {ROUTE_TIMEOUT_S}s waiting for /info's ↳ continuation room line "
            f"(muted #888888 marker + Room:) — SendTaggedLines multi-line output is wrong."
        )
    if lobby_name not in room:
        return False, f"/info room continuation line didn't mention the test lobby '{lobby_name}': {room}"
    if "[INFO]" in room:
        return False, (
            f"/info continuation room line still carries the [INFO] tag — the 'identifier once' "
            f"behavior regressed (tag should appear only on the first line): {room}"
        )

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
