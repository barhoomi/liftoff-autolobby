"""Scenario: prove the color-theming FormatTag helper is actually applied to chat output.

A client sends /info; the server responds with an [INFO] message. This scenario asserts
that the server's chat_response event carries the *themed markup* produced by
`FormatTag("INFO", activeTheme.infoTagColor)` — a bold+color block around [INFO] using the
default `infoTagColor` (#0000FF) — i.e.

    <b><color=#0000FF>[INFO]</color></b> ...

This is the automatable proxy for the human-only "colors render on screen" check: it proves
the theme helper wraps the tag with the configured hex (not the old raw `<color=#0000FF>[INFO]</color>`
without bold, and not a hardcoded string), so the theme system is wired into output. Actual
on-screen rendering of that markup stays a human visual item.

See docs/features/doing/color-theming.md and automated-testing.md Phase 3.

Note: LogEvent's JsonEscapeStrict only escapes \\ " \\n \\r \\t — angle brackets, '#', and
'[' pass through verbatim, so the markup appears literally in the JSONL line and a plain
regex match is valid (confirmed against plugin/Plugin.cs JsonEscapeStrict).
"""
import scenario_harness as harness

CLIENT_NICKNAME = "ThemeProbe"
CLIENT_SCRIPT = ["5 /info"]
ASSERTION_TIMEOUT_S = 30

# The exact themed prefix FormatTag("INFO", "#0000FF") emits: <b><color=#0000FF>[INFO]</color></b>
THEMED_INFO_PATTERN = r'"event":"chat_response".*<b><color=#0000FF>\[INFO\]</color></b>'


def check(server_log_path, lobby_name):
    line = harness.wait_for_log_pattern(server_log_path, THEMED_INFO_PATTERN, ASSERTION_TIMEOUT_S)
    if line is None:
        return False, (
            f"Timed out after {ASSERTION_TIMEOUT_S}s waiting for a themed [INFO] chat_response "
            f"(bold+color #0000FF block) in {server_log_path}. Either /info was not processed, "
            f"or FormatTag is not being applied to [INFO] output."
        )
    return True, (
        f"Server's [INFO] chat_response carries the themed FormatTag markup "
        f"(<b><color=#0000FF>[INFO]</color></b>): {line}"
    )
