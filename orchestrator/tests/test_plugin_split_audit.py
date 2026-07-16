"""
Code-motion audit for the Plugin.cs -> Plugin.<Area>.cs partial-class split
(docs/features/doing/plugin-decomposition.md).

This is the mechanical proof the split's verification plan requires: it
re-derives every top-level member (field/const/method/nested class) declared
directly inside `AutoLobbyPlugin` from a FROZEN pre-split snapshot of
Plugin.cs (fixtures/plugin_cs_pre_split_snapshot.cs, captured at commit
052f888 -- the last commit before the split started), and from the current
post-split file set (plugin/Plugin.cs + plugin/Plugin.*.cs). It asserts:

  (a) every symbol from the pre-split file appears EXACTLY ONCE across the
      post-split file set (no duplication, no silent drop),
  (b) every such symbol's body is identical to the pre-split version after
      whitespace normalization (collapinsg all runs of whitespace to a
      single space) -- this is what "byte-identical, pure code motion"
      means in practice: comments and code are preserved verbatim, only
      the surrounding blank-line/indentation trivia at file-join seams may
      differ,
  (c) no symbol exists in the post-split set that wasn't in the pre-split
      set (nothing invented, nothing duplicated under a new name).

This test does NOT need the game, BepInEx, or any Photon/Unity DLL -- it
operates purely on the C# source text. It intentionally does NOT use `git`
at run time (no dependency on merge-base/branch state, which would break
after this branch merges to main) -- the frozen fixture is the durable
reference.

NOT a general C# parser: see the parsing helpers' docstrings for the exact,
narrow feature set this codebase actually uses (interpolated strings with
{{ / }} escapes and interpolation holes, comments, char literals, and
brace-initializer fields terminated by ';'). If Plugin.cs source style ever
changes in a way this parser can't handle, this test will fail loudly with
a parse error rather than silently mis-comparing -- that is the intended
failure mode.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugin")
FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "plugin_cs_pre_split_snapshot.cs")


# ---------------------------------------------------------------------------
# Minimal C#-source scanner (string/comment/char-literal aware) -- shared by
# both the class-body boundary finder and the top-level member splitter.
# ---------------------------------------------------------------------------

def is_string_start(s: str, i: int) -> bool:
    n = len(s)
    j = i
    while j < n and s[j] in "@$":
        j += 1
    return j < n and s[j] == '"'


def skip_char_literal(s: str, i: int) -> int:
    n = len(s)
    i += 1
    if i < n and s[i] == "\\":
        i += 2
    else:
        i += 1
    if i < n and s[i] == "'":
        i += 1
    return i


def skip_hole(s: str, i: int) -> int:
    """i is positioned right after the '{' that opened an interpolation hole."""
    n = len(s)
    depth = 0
    while i < n:
        c = s[i]
        if is_string_start(s, i):
            i = skip_string_literal(s, i)
            continue
        if c == "'":
            i = skip_char_literal(s, i)
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]":
            depth -= 1
            i += 1
            continue
        if c == "}":
            if depth == 0:
                return i + 1
            depth -= 1
            i += 1
            continue
        i += 1
    return i


def skip_string_literal(s: str, i: int) -> int:
    n = len(s)
    verbatim = False
    interpolated = False
    while i < n and s[i] in "@$":
        if s[i] == "@":
            verbatim = True
        if s[i] == "$":
            interpolated = True
        i += 1
    assert s[i] == '"'
    i += 1
    while i < n:
        c = s[i]
        if verbatim and c == '"':
            if i + 1 < n and s[i + 1] == '"':
                i += 2
                continue
            return i + 1
        if not verbatim and c == "\\":
            i += 2
            continue
        if not verbatim and c == '"':
            return i + 1
        if interpolated and c == "{":
            if i + 1 < n and s[i + 1] == "{":
                i += 2
                continue
            i = skip_hole(s, i + 1)
            continue
        if interpolated and c == "}":
            if i + 1 < n and s[i + 1] == "}":
                i += 2
                continue
            i += 1
            continue
        i += 1
    return i


def peek_next_significant(s: str, i: int):
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = j if j != -1 else n
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        return c, i
    return None, i


def find_matching_close(s: str, open_idx: int) -> int:
    n = len(s)
    i = open_idx + 1
    depth = 1
    while i < n:
        c = s[i]
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = j if j != -1 else n
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        if is_string_start(s, i):
            i = skip_string_literal(s, i)
            continue
        if c == "'":
            i = skip_char_literal(s, i)
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    raise ValueError("no matching close brace found")


def split_class_body_members(body: str):
    """Split a class body into (start, end) ranges, each one top-level member
    INCLUDING its leading trivia. Concatenating body[s:e] for all ranges
    reconstructs `body` exactly."""
    n = len(body)
    i = 0
    depth = 0
    member_start = 0
    members = []
    while i < n:
        c = body[i]
        if c == "/" and i + 1 < n and body[i + 1] == "/":
            j = body.find("\n", i)
            i = j if j != -1 else n
            continue
        if c == "/" and i + 1 < n and body[i + 1] == "*":
            j = body.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        if is_string_start(body, i):
            i = skip_string_literal(body, i)
            continue
        if c == "'":
            i = skip_char_literal(body, i)
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                nxt, _ = peek_next_significant(body, i)
                if nxt == ";":
                    continue
                members.append((member_start, i))
                member_start = i
            continue
        if c == ";" and depth == 0:
            i += 1
            members.append((member_start, i))
            member_start = i
            continue
        i += 1
    if member_start < n:
        members.append((member_start, n))
    return members


NAME_DECL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(\(|=|;|\{)")


def declared_name(chunk_text: str) -> str:
    """Best-effort identifier for a member chunk: the identifier immediately
    preceding the first '(', '=', ';' or '{' on its first non-trivial line.
    Used purely as a lookup KEY, not to validate C# syntax."""
    for raw_line in chunk_text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("[") or line.startswith("/*") or line.startswith("*"):
            continue
        m = NAME_DECL_RE.search(line)
        if m:
            return m.group(1)
        return line[:60]
    return "<empty>"


def extract_class_members(text: str):
    """Given a full .cs file's text containing `class AutoLobbyPlugin`,
    return {name: normalized_body} for every top-level member declared
    directly in that class body."""
    lines = text.split("\n")
    class_decl_idx = None
    for i, l in enumerate(lines):
        if re.search(r"\bclass\s+AutoLobbyPlugin\b", l):
            class_decl_idx = i
            break
    assert class_decl_idx is not None, "no `class AutoLobbyPlugin` declaration found"
    char_offset = sum(len(l) + 1 for l in lines[:class_decl_idx])
    open_brace_idx = text.index("{", char_offset)
    close_idx = find_matching_close(text, open_brace_idx)
    body = text[open_brace_idx + 1:close_idx]

    members = split_class_body_members(body)
    result = {}
    for s, e in members:
        chunk = body[s:e]
        if not chunk.strip():
            continue  # trailing whitespace-only trivia before the class's own closing brace
        name = declared_name(chunk)
        normalized = re.sub(r"\s+", " ", chunk).strip()
        result[name] = normalized
    return result


def _reconstruct_and_check_roundtrip(text: str):
    """Sanity self-check: re-joining every extracted member chunk (using the
    RAW, non-normalized ranges) must reproduce the original class body
    exactly. Guards against a parser bug silently eating characters."""
    lines = text.split("\n")
    class_decl_idx = next(i for i, l in enumerate(lines) if re.search(r"\bclass\s+AutoLobbyPlugin\b", l))
    char_offset = sum(len(l) + 1 for l in lines[:class_decl_idx])
    open_brace_idx = text.index("{", char_offset)
    close_idx = find_matching_close(text, open_brace_idx)
    body = text[open_brace_idx + 1:close_idx]
    members = split_class_body_members(body)
    reconstructed = "".join(body[s:e] for s, e in members)
    assert reconstructed == body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pre_split_members():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        text = f.read()
    _reconstruct_and_check_roundtrip(text)
    return extract_class_members(text)


@pytest.fixture(scope="module")
def post_split_files():
    """plugin/Plugin.cs plus every plugin/Plugin.<Area>.cs -- explicitly NOT
    CommandRegistry.cs / IChatCommand.cs / EventLog.cs / Commands/*.cs, which
    predate this split and are out of its scope."""
    paths = sorted(glob.glob(os.path.join(PLUGIN_DIR, "Plugin*.cs")))
    assert paths, "no Plugin*.cs files found -- has the plugin/ layout changed?"
    return paths


@pytest.fixture(scope="module")
def post_split_members(post_split_files):
    """name -> (body, [source files it was found in])"""
    occurrences = {}
    for path in post_split_files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        _reconstruct_and_check_roundtrip(text)
        members = extract_class_members(text)
        for name, body in members.items():
            occurrences.setdefault(name, []).append((os.path.basename(path), body))
    return occurrences


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

def test_expected_split_files_exist():
    expected = [
        "Plugin.cs", "Plugin.Config.cs", "Plugin.Chat.cs", "Plugin.Rotation.cs",
        "Plugin.GameRoom.cs", "Plugin.Photon.cs", "Plugin.Harmony.cs",
        "Plugin.UiToolkit.cs", "Plugin.Navigation.cs", "Plugin.RoomSetup.cs",
    ]
    present = {os.path.basename(p) for p in sorted(glob.glob(os.path.join(PLUGIN_DIR, "Plugin*.cs")))}
    missing = [e for e in expected if e not in present]
    assert not missing, f"expected split file(s) missing: {missing}"


def test_every_pre_split_symbol_appears_exactly_once(pre_split_members, post_split_members):
    missing = []
    duplicated = []
    for name in pre_split_members:
        occs = post_split_members.get(name, [])
        if len(occs) == 0:
            missing.append(name)
        elif len(occs) > 1:
            duplicated.append((name, [f for f, _ in occs]))
    assert not missing, f"symbol(s) dropped by the split (not found in any post-split file): {missing}"
    assert not duplicated, f"symbol(s) duplicated across split files: {duplicated}"


def test_every_pre_split_symbol_body_is_unchanged(pre_split_members, post_split_members):
    mismatches = []
    for name, pre_body in pre_split_members.items():
        occs = post_split_members.get(name)
        if not occs:
            continue  # already reported by the "dropped" test above
        post_file, post_body = occs[0]
        if post_body != pre_body:
            mismatches.append((name, post_file))
    assert not mismatches, (
        "symbol body changed during the split (whitespace-normalized comparison) "
        f"for: {mismatches} -- pure code motion must not alter bodies"
    )


def test_no_symbols_invented_by_the_split(pre_split_members, post_split_members):
    extra = sorted(set(post_split_members) - set(pre_split_members))
    assert not extra, f"symbol(s) present after the split that didn't exist before it: {extra}"


def test_each_new_partial_file_is_partial_class_autolobbyplugin(post_split_files):
    for path in post_split_files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        assert re.search(r"\bpartial\s+class\s+AutoLobbyPlugin\b", text), (
            f"{os.path.basename(path)} does not declare `partial class AutoLobbyPlugin`"
        )
        if os.path.basename(path) != "Plugin.cs":
            # Only the primary declaration (Plugin.cs) may carry the base class /
            # BepInPlugin attribute; every other partial must be a bare partial
            # declaration (no visibility/signature surface added).
            assert "BaseUnityPlugin" not in text, (
                f"{os.path.basename(path)} unexpectedly repeats the base class "
                "declaration -- only Plugin.cs should declare `: BaseUnityPlugin`"
            )


def test_new_files_carry_a_mode_header(post_split_files):
    for path in post_split_files:
        if os.path.basename(path) == "Plugin.cs":
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        assert re.search(r"//\s*MODE:\s*(shared|server-only)", text), (
            f"{os.path.basename(path)} is missing the '// MODE: shared|server-only' header"
        )


def test_line_count_targets():
    plugin_cs = os.path.join(PLUGIN_DIR, "Plugin.cs")
    with open(plugin_cs, encoding="utf-8") as f:
        n = sum(1 for _ in f)
    # Soft-ish target from the feature doc (~600); RunTick was kept whole (see the
    # doc's "Deviations" section) rather than split, so allow some slack instead of
    # hard-failing a few lines over -- but a regression back toward monolith size
    # should still fail loudly.
    assert n <= 700, f"Plugin.cs has grown to {n} lines -- expected roughly ~500-600"

    for path in sorted(glob.glob(os.path.join(PLUGIN_DIR, "Plugin.*.cs"))):
        with open(path, encoding="utf-8") as f:
            n = sum(1 for _ in f)
        assert n <= 1000, f"{os.path.basename(path)} has {n} lines, over the ~1000 target"
