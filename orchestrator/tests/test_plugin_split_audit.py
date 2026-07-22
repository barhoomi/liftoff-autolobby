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

Extended 2026-07-16 (integration/decomp-democracy, merging feature/democracy-
skip on top of this split): the frozen pre-split snapshot necessarily
predates any feature merged after the split, so a handful of symbols that
democracy-skip added/extended fail the naive "identical to pre-split" and
"nothing invented" checks by design. See PINNED_POST_SPLIT_BODIES and
test_pinned_post_split_bodies_match_exactly below for how those are carved
out WITHOUT weakening the checks for every other symbol, and without simply
exempting the 8 affected names from scrutiny forever.
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


# Post-split feature deltas layered on top of the decomposition. The frozen baseline
# snapshot captures Plugin.cs the instant BEFORE the split (052f888) -- it proves the
# split itself was pure code motion, but it was never meant to freeze Plugin.cs's
# *content* forever. Normal feature work continues to land on top of the decomposed
# layout exactly as it would have landed on the old monolith.
#
# Contributors so far:
#   - feature/democracy-skip (merged 2026-07-16, docs/features/.../democracy-skip.md):
#     3 brand-new symbols + appended lines to 5 pre-existing pre-split symbols.
#   - feature/plugin-mode-split (2026-07-22, docs/features/doing/plugin-mode-split.md,
#     R3 of public-release-v1): the server|client role split. Introduced the settings-
#     source abstraction (ISettingsSource + File/Config sources), role-aware IsAdmin,
#     the client-mode tick gates (RunServerMaintenanceTick extraction, HandleClientTick,
#     NoteTickFailure), and the ConfigureAndCreateRoom create/update split
#     (ApplyRoomSettingsPopup) -- ~25 new symbols and edits to ~15 pre-split ones.
#
# Rather than blanket-exempting these names from the audit (which would silently
# swallow ANY future edit to them, not just the documented change -- exactly the kind
# of drift this audit exists to catch), their exact current (whitespace-normalized)
# bodies are pinned here and checked verbatim by test_pinned_post_split_bodies_match_
# exactly below. A future, undocumented change to any of them will still fail this
# audit; an intentional one must update the pinned value here AND record why in the
# relevant feature doc.
PINNED_POST_SPLIT_BODIES = {
    'ApplyBotNicknameIfNeeded': '// Sets PhotonNetwork.NickName once the Photon assembly is resolvable. Retried from // RunTick() (not called from Awake) because Photon\'s static classes aren\'t reliably // loaded that early in BepInEx\'s boot sequence — same reasoning as the other reflective // PhotonNetwork accessors below, which are also called per-tick rather than once. private static void ApplyBotNicknameIfNeeded() { // DANGER gate (plugin-mode-split.md): never rename a real player. Client mode must // never touch PhotonNetwork.NickName. (botNickname is also never loaded in client // mode, so this is belt-and-suspenders.) if (IsClientMode) return; if (nicknameApplied || string.IsNullOrEmpty(botNickname)) return; try { Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? Type.GetType("PhotonNetwork, Assembly-CSharp"); if (type != null) { PropertyInfo prop = type.GetProperty("NickName", BindingFlags.Public | BindingFlags.Static); if (prop != null && prop.CanWrite) { prop.SetValue(null, botNickname); nicknameApplied = true; UnityEngine.Debug.Log($"[AutoLobbyPlugin] Applied bot nickname: \'{botNickname}\'"); } } } catch (Exception ex) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to apply bot nickname: {ex.Message}"); } }',
    'ApplyRoomSettingsPopup': '// Shared "apply settings to the current room" path (plugin-mode-split.md, R3). Selects the // target environment / game mode / track in the settings popup and submits it. Used by // rotation in BOTH roles: server via ConfigureAndCreateRoom (allowCreate:true), and client // once rotation is engaged (allowCreate:false, R4). allowCreate gates the ONE server-only // action — clicking Create Game to host a NEW room. With allowCreate:false only the Update // button (applying settings to the room you are already in) is ever clicked, so it can // never create or hijack a room the player is hosting. private static void ApplyRoomSettingsPopup(PopupQuickPlayMultiplayerSetup popup, bool allowCreate) { if (string.IsNullOrEmpty(targetTrackName)) { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] targetTrackName is empty, canceling popup."); Button cancelBtn = GetPopupCancelButton(popup); if (cancelBtn != null) cancelBtn.onClick.Invoke(); return; } // Dump button listeners for debugging if (DateTime.Now.Second % 5 == 0) { DumpButtonListeners("buttonCreateGame", GetPopupCreateButton(popup)); DumpButtonListeners("buttonUpdateGame", GetPopupUpdateButton(popup)); var fieldActive = typeof(PopupQuickPlayMultiplayerSetup).GetField("activeButton", BindingFlags.NonPublic | BindingFlags.Instance); if (fieldActive != null) { Button activeBtnDebug = (Button)fieldActive.GetValue(popup); DumpButtonListeners("activeButton", activeBtnDebug); } } // 1. Configure Room settings RoomSettingsPanel roomSettings = GetPopupRoomSettings(popup); if (roomSettings != null) { bool makePrivate = true; string privacyPath = Path.Combine(pluginPath, "room_private.txt"); if (File.Exists(privacyPath)) { string content = File.ReadAllText(privacyPath).Trim(); if (content.Equals("false", StringComparison.OrdinalIgnoreCase)) { makePrivate = false; } } // Set toggle first so the name panel activates before we write to the InputField. // The room name InputField lives inside panelPrivateRoom which starts inactive; // Unity InputField.onValueChanged does not fire reliably on inactive objects. Toggle togglePrivate = GetRoomSettingsTogglePrivate(roomSettings); if (togglePrivate != null && togglePrivate.isOn != makePrivate) { togglePrivate.isOn = makePrivate; } if (makePrivate) { InputField inputRoomName = GetRoomSettingsInputField(roomSettings); if (inputRoomName != null && inputRoomName.text != targetLobbyName) { inputRoomName.text = targetLobbyName; } } } // 2. Configure Content settings (Environment, GameMode, Track) // Environment is set FIRST: selecting it can cause the game to re-filter/rebuild // the GameMode and Content dropdown options (cascading filters), so GameMode must // be (re)verified AFTER Environment, not before, or a valid choice can be silently // invalidated and never reapplied. // (Declared here — the availability dump\'s own contentSettings lives in // ConfigureAndCreateRoom now that the method is split.) ContentSettingsPanel contentSettings = GetPopupContentSettings(popup); if (contentSettings != null) { // Set Environment LiftoffDropdown dropdownEnvironment = GetContentDropdownEnvironment(contentSettings); if (dropdownEnvironment != null) { int envIndex = -1; for (int i = 0; i < dropdownEnvironment.options.Count; i++) { if (dropdownEnvironment.options[i].text.Equals(targetEnvironment, StringComparison.OrdinalIgnoreCase)) { envIndex = i; break; } } if (envIndex != -1) { if (dropdownEnvironment.value != envIndex) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Changing Environment dropdown to: {targetEnvironment}"); dropdownEnvironment.value = envIndex; dropdownEnvironment.onValueChanged.Invoke(envIndex); return; // Let the UI update next frame } } else { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Environment \'{targetEnvironment}\' not found in dropdown. Available Environments:"); for (int i = 0; i < dropdownEnvironment.options.Count; i++) { UnityEngine.Debug.Log($" - Env {i}: \'{dropdownEnvironment.options[i].text}\'"); } } } } // Log all dropdown options for debugging if (DateTime.Now.Second % 15 == 0) { LiftoffDropdown dropdownEnvironmentDebug = GetContentDropdownEnvironment(contentSettings); if (dropdownEnvironmentDebug != null) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Environment dropdown options (Count: {dropdownEnvironmentDebug.options.Count}, Current Value: {dropdownEnvironmentDebug.value}):"); for (int i = 0; i < dropdownEnvironmentDebug.options.Count; i++) { UnityEngine.Debug.Log($" - Env Option {i}: \'{dropdownEnvironmentDebug.options[i].text}\'"); } } LiftoffDropdown dropdownContentDebug = GetContentDropdownContent(contentSettings); if (dropdownContentDebug != null) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Content dropdown options (Count: {dropdownContentDebug.options.Count}, Current Value: {dropdownContentDebug.value}):"); for (int i = 0; i < dropdownContentDebug.options.Count; i++) { UnityEngine.Debug.Log($" - Content Option {i}: \'{dropdownContentDebug.options[i].text}\'"); } } } // Set GameMode (after Environment, since Environment selection can re-filter these options) LiftoffDropdown dropdownGameMode = GetContentDropdownGameMode(contentSettings); if (dropdownGameMode != null) { int modeIndex = -1; for (int i = 0; i < dropdownGameMode.options.Count; i++) { if (dropdownGameMode.options[i].text.Equals(targetGameMode, StringComparison.OrdinalIgnoreCase)) { modeIndex = i; break; } } if (modeIndex != -1) { if (dropdownGameMode.value != modeIndex) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Changing GameMode dropdown to: {targetGameMode}"); dropdownGameMode.value = modeIndex; dropdownGameMode.onValueChanged.Invoke(modeIndex); return; // Let the UI update next frame } } else { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] GameMode \'{targetGameMode}\' not found in dropdown. Available GameModes:"); for (int i = 0; i < dropdownGameMode.options.Count; i++) { UnityEngine.Debug.Log($" - GameMode {i}: \'{dropdownGameMode.options[i].text}\'"); } } } // Set Track LiftoffDropdown dropdownContent = GetContentDropdownContent(contentSettings); if (dropdownContent != null) { int trackIndex = -1; for (int i = 0; i < dropdownContent.options.Count; i++) { if (dropdownContent.options[i].text.IndexOf(targetTrackName, StringComparison.OrdinalIgnoreCase) >= 0) { trackIndex = i; break; } } if (trackIndex != -1) { if (dropdownContent.value != trackIndex) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Changing Track dropdown to: {targetTrackName} (index: {trackIndex})"); dropdownContent.value = trackIndex; dropdownContent.onValueChanged.Invoke(trackIndex); return; // Let the UI update next frame } } else { double popupAge = (DateTime.Now - popupOpenedTime).TotalSeconds; // Once, try switching to the Custom content tab if (!triedCustomContentTab && popupAge > 3.0) { triedCustomContentTab = true; TrySelectCustomContentTab(popup); return; // Let dropdown refresh next tick } // Timeout: cancel popup and advance rotation if (popupAge > 45.0) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Track \'{targetTrackName}\' not found after {popupAge:F0}s — skipping."); Button cancelBtn = GetPopupCancelButton(popup); if (cancelBtn != null && cancelBtn.gameObject.activeInHierarchy) cancelBtn.onClick.Invoke(); targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode); triedCustomContentTab = false; isSubmittingSettings = false; return; } if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Track \'{targetTrackName}\' not found in dropdown ({popupAge:F0}s). Available options:"); for (int i = 0; i < dropdownContent.options.Count; i++) UnityEngine.Debug.Log($" - Option {i}: \'{dropdownContent.options[i].text}\'"); } return; } } } // 3. Click the active button or fallback to Create/Update based on room state var fieldActiveButton = typeof(PopupQuickPlayMultiplayerSetup).GetField("activeButton", BindingFlags.NonPublic | BindingFlags.Instance); Button activeBtn = fieldActiveButton != null ? (Button)fieldActiveButton.GetValue(popup) : null; bool settingsValid = roomSettings != null && roomSettings.GameSettingsValid() && contentSettings != null && contentSettings.GameSettingsValid(); // The game\'s own "active button" is Create when hosting a new room and Update when in // one. It is server-only (gated by allowCreate): a client rotation must never risk // clicking Create, so it skips straight to the explicit in-room Update path below. if (allowCreate && activeBtn != null && activeBtn.gameObject.activeInHierarchy) { if (activeBtn.interactable || settingsValid) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking activeButton: {activeBtn.name} (interactable={activeBtn.interactable}, settingsValid={settingsValid})"); popupSubmittedTime = DateTime.Now; isSubmittingSettings = true; activeBtn.onClick.Invoke(); CaptureLoadedTrack(); // Reset room timer if we are in a room GameObject gameRoomObj = GameObject.Find("GameRoom"); bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy); if (inRoom) { roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; chatWarnedAboutNextRace = false; } return; } } if (settingsValid) { // Check if GameRoom is active GameObject gameRoomObj = GameObject.Find("GameRoom"); bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy); if (inRoom) { // We are in a room, we want to update settings. SHARED — this is the Update // button both server rotation and client rotation use to apply a track change. Button updateBtn = GetPopupUpdateButton(popup); if (updateBtn != null && updateBtn.gameObject.activeInHierarchy && updateBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Fallback: Clicking Update Game settings button inside lobby."); popupSubmittedTime = DateTime.Now; isSubmittingSettings = true; updateBtn.onClick.Invoke(); CaptureLoadedTrack(); roomCreatedTime = DateTime.Now; // Reset the rotation timer! lastActivityTime = DateTime.UtcNow; chatWarnedAboutNextRace = false; } } else if (allowCreate) { // We are not in a room, we want to create a new room. SERVER-ONLY: hosting a // new room is not something a client build ever does. (With allowCreate:false // and no room, nothing is clicked — the player has to be in a room they host.) Button createBtn = GetPopupCreateButton(popup); if (createBtn != null && createBtn.gameObject.activeInHierarchy && createBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Fallback: Clicking Create Game button to host new room."); popupSubmittedTime = DateTime.Now; isSubmittingSettings = true; createBtn.onClick.Invoke(); CaptureLoadedTrack(); } } } }',
    'Awake': 'private void Awake() { Logger.LogInfo("[AutoLobbyPlugin] BepInEx Awake called!"); try { pluginPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "BepInEx", "plugins"); // Startup diagnostic (windows-compatibility.md R2): the single most load-bearing // path assumption in the plugin — surfaced explicitly so the Windows verification // pass can confirm it resolved to the right place. Logger.LogInfo($"[AutoLobbyPlugin] pluginPath resolved to: {pluginPath}"); // Resolve role (server|client) and pick the single settings source BEFORE any // reader runs (plugin-mode-split.md). Config is the BaseUnityPlugin ConfigFile. InitializeRoleAndSettings(Config); LoadThemeConfig(); // Server-automation-only config: admin list, Liftoff Pro sign-in toggle/nickname/ // credentials, and the scenario-harness client script. Client mode never runs the // menu automation that reads these, and its admin is the local player — so it does // not touch these orchestrator files at all (one source per role, no merge). if (IsServerMode) { LoadAdminIds(); LoadUseLiftoffPro(); LoadBotNickname(); LoadLiftoffProCredentials(); LoadClientScript(); } else { UnityEngine.Debug.Log("[AutoLobbyPlugin] Client mode: admin = local player; skipping orchestrator config files (admin_ids/use_liftoff_pro/bot_nickname/credentials/client_script)."); } // Load initial shuffle mode shuffleMode = GetShuffleMode(); Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial shuffleMode: {shuffleMode}"); // Load initial democracy mode (democracy-skip.md) democracyEnabled = GetDemocracyMode(); Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial democracyEnabled: {democracyEnabled}"); // Register all chat commands with the command registry (replaces the old // hardcoded HandleChatCommand switch). CommandRegistry.Initialize(); // Apply Harmony patches to fix database loading exceptions ApplyHarmonyPatches(); // Dynamically resolve multiplayer client connection check method ResolveMultiplayerClientCheck(); // Subscribe to the static Canvas render event (runs on main thread every frame) Canvas.willRenderCanvases += OnWillRenderCanvases; // Known long-standing Liftoff quirk (both Pro and anonymous sign-in): the // sign-in flow can get stuck reporting "An authentication request is still // pending. Cannot connect." on every further click, seemingly forever, until // the MultiplayerMenu scene is torn down and reloaded (previously worked // around by hand: back to MainMenu, back into Multiplayer). Hooking Unity\'s // log callback lets HandleMultiplayerMenu() detect this the moment it fires // and trigger that same recovery automatically instead of hanging. Application.logMessageReceived += OnUnityLogMessageReceived; Logger.LogInfo("[AutoLobbyPlugin] Static Canvas.willRenderCanvases hook registered successfully!"); } catch (Exception ex) { Logger.LogError($"[AutoLobbyPlugin] Static hook registration failed: {ex.Message}"); } }',
    'CaptureLoadedTrack': '// Called at each settings-popup submit-success point (the same instant the track genuinely // becomes the loaded one). Snapshots the just-submitted target as the CURRENT track for // /info. (Track history is appended here too — see the /history slice.) private static void CaptureLoadedTrack() { currentTrackName = targetTrackName; currentEnvironment = targetEnvironment; trackHistory.Add($"{targetEnvironment} - {targetTrackName}"); while (trackHistory.Count > TrackHistoryMax) trackHistory.RemoveAt(0); // democracy-skip.md: a new track just loaded — stale skip votes from the // previous track must not carry over. skipVotes.Clear(); }',
    'ClientTickFailureLimit': 'private const int ClientTickFailureLimit = 15;',
    'ConfigureAndCreateRoom': 'private static void ConfigureAndCreateRoom(PopupQuickPlayMultiplayerSetup popup) { ContentSettingsPanel contentSettings = GetPopupContentSettings(popup); if (contentSettings != null) { if (!isDumpingTrackModes && !trackModeDumpDoneThisSession) { if (TryReuseCachedTrackModeDump(contentSettings)) { trackModeDumpDoneThisSession = true; } else { isDumpingTrackModes = true; dumpEnvIndex2 = 0; dumpModeIndex2 = 0; dumpedTrackModeMap.Clear(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Starting Environment x GameMode track availability dump ({TrackModeDumpCandidateModes.Length} candidate modes)..."); } } if (isDumpingTrackModes) { LiftoffDropdown dropdownEnvironment = GetContentDropdownEnvironment(contentSettings); LiftoffDropdown dropdownGameMode = GetContentDropdownGameMode(contentSettings); LiftoffDropdown dropdownContent = GetContentDropdownContent(contentSettings); if (dropdownEnvironment != null && dropdownGameMode != null && dropdownContent != null) { if (dumpEnvIndex2 < dropdownEnvironment.options.Count) { if (dropdownEnvironment.value != dumpEnvIndex2) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Dump: Selecting environment index {dumpEnvIndex2} ({dropdownEnvironment.options[dumpEnvIndex2].text})"); dropdownEnvironment.value = dumpEnvIndex2; dropdownEnvironment.onValueChanged.Invoke(dumpEnvIndex2); return; // Let the UI update next frame } string envName = dropdownEnvironment.options[dumpEnvIndex2].text; if (dumpModeIndex2 < TrackModeDumpCandidateModes.Length) { string modeName = TrackModeDumpCandidateModes[dumpModeIndex2]; int modeIdx = -1; for (int i = 0; i < dropdownGameMode.options.Count; i++) { if (dropdownGameMode.options[i].text.Equals(modeName, StringComparison.OrdinalIgnoreCase)) { modeIdx = i; break; } } if (modeIdx == -1) { // This environment doesn\'t offer this mode at all RecordTrackModeDump(envName, modeName, new List<string>()); dumpModeIndex2++; return; } if (dropdownGameMode.value != modeIdx) { dropdownGameMode.value = modeIdx; dropdownGameMode.onValueChanged.Invoke(modeIdx); return; // Let the UI update next frame } var tracks = new List<string>(); for (int i = 0; i < dropdownContent.options.Count; i++) { tracks.Add(dropdownContent.options[i].text); } RecordTrackModeDump(envName, modeName, tracks); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Dump: env=\'{envName}\' mode=\'{modeName}\' -> {tracks.Count} tracks"); dumpModeIndex2++; return; // Wait for next tick } else { dumpModeIndex2 = 0; dumpEnvIndex2++; return; } } else { WriteLegacyUiTracksDump(dumpedTrackModeMap); WriteTrackModeAvailabilityDump(dumpedTrackModeMap); isDumpingTrackModes = false; trackModeDumpDoneThisSession = true; UnityEngine.Debug.Log("[AutoLobbyPlugin] Track/mode availability dump complete."); } } return; // Pause room configuration during dumping } } // The Environment x GameMode availability dump above feeds the orchestrator and is // server-only. Everything below is the SHARED apply-and-submit path; allowCreate:true // reproduces the original create-or-update behavior exactly (server mode). ApplyRoomSettingsPopup(popup, allowCreate: true); }',
    'GetAutoStart': 'private static bool GetAutoStart() => Settings.AutoStart;',
    'GetDemocracyMode': '// democracy-skip.md: whether /skip is a public majority vote (true) or admin-only (false). private static bool GetDemocracyMode() => Settings.DemocracyMode;',
    'GetKeepAliveInterval': 'private static double GetKeepAliveInterval() => Settings.KeepAliveSeconds;',
    'GetLocalPhotonUserId': 'private static string GetLocalPhotonUserId() { if (!string.IsNullOrEmpty(cachedLocalPhotonUserId)) return cachedLocalPhotonUserId; try { Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? Type.GetType("PhotonNetwork, Assembly-CSharp"); if (type != null) { PropertyInfo localPlayerProp = type.GetProperty("LocalPlayer", BindingFlags.Public | BindingFlags.Static); object localPlayer = localPlayerProp?.GetValue(null); if (localPlayer != null) { PropertyInfo userIdProp = localPlayer.GetType().GetProperty("UserId", BindingFlags.Public | BindingFlags.Instance); string uid = userIdProp?.GetValue(localPlayer) as string; if (!string.IsNullOrEmpty(uid)) cachedLocalPhotonUserId = uid; } } } catch (Exception ex) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] GetLocalPhotonUserId failed: {ex.Message}"); } return cachedLocalPhotonUserId; }',
    'GetOverrideGameMode': 'private static string GetOverrideGameMode() => Settings.OverrideGameMode;',
    'GetRotationInterval': '// The following six readers now delegate to the active ISettingsSource (server = the // text files, unchanged; client = the BepInEx ConfigFile). Every call site keeps its // original name and semantics; only the source of the value moved behind the abstraction. private static double GetRotationInterval() => Settings.RotationIntervalSeconds;',
    'GetShuffleMode': 'private static bool GetShuffleMode() => Settings.ShuffleMode;',
    'HandleClientTick': "// Client-mode tick (plugin-mode-split.md, R3). Reached only via the IsClientMode branch // in RunTick, so none of the server automation (menus, sign-in, room creation, maintenance // quit, nickname) can run. At R3 this is intentionally inert — the plugin does nothing in // the player's game until the player engages rotation with a lifecycle command // (client-lifecycle-commands.md, R4 flips clientRotationEngaged via /start). Only once // engaged does it drive the SHARED rotation loop (HandleGameRoom / HandleFlightLevel / the // settings-popup Update path) in a room the player already hosts. It must NEVER click a // button the player did not ask for while idle. private static void HandleClientTick(string sceneName) { if (!clientRotationEngaged) return; // (R4) engaged client rotation is wired here — reusing the shared in-room handlers. }",
    'HandleGameRoom': 'private static void HandleGameRoom() { if (isLeaving) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Currently leaving room, ignoring GameRoom tick."); return; } // Flush any chat messages that were queued while the chat panel wasn\'t available yet // (e.g. sent from a Photon callback while still on the MultiplayerMenu screen). if (pendingRoomChatMessages.Count > 0) { string[] toSend = pendingRoomChatMessages.ToArray(); pendingRoomChatMessages.Clear(); foreach (string msg in toSend) { SendChatMessage(msg); } } if (roomCreatedTime == DateTime.MinValue || roomCreatedTime == DateTime.MaxValue) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Entered GameRoom. Starting room timer."); LogEvent("room_entered"); roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; chatWarnedAboutNextRace = false; firstStartGameClickTime = DateTime.MinValue; // Apply a persisted max-players override (survives bot restarts), if one is configured. string maxPlayersPath = Path.Combine(pluginPath, "max_players.txt"); if (File.Exists(maxPlayersPath)) { int configuredMax; if (int.TryParse(File.ReadAllText(maxPlayersPath).Trim(), out configuredMax)) { int applied; string err; if (!SetRoomMaxPlayers(configuredMax, out applied, out err)) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to apply persisted max_players.txt ({configuredMax}): {err}"); } } } } // democracy-skip.md: prune skip votes from players who left the room, then // re-evaluate the majority (a departure can lower the required threshold enough // for the remaining votes to now win). No-ops when democracy mode is off. SkipCommand.CheckDisconnectedVoters(); double elapsed = (DateTime.Now - roomCreatedTime).TotalSeconds; ProcessClientScript(elapsed); // Auto-start: click the START button 15 seconds after entering the room to give players time to join if (GetAutoStart() && elapsed >= 15.0 && (DateTime.Now - lastStartGameClickedTime).TotalSeconds > 30.0) { string[] startNames = { "buttonStartGame", "btnStartGame", "StartGame", "btnStart", "buttonStart" }; Button startBtn = FindButtonByTextOrName("START GAME", startNames); if (startBtn == null) startBtn = FindButtonByTextOrName("START", startNames); if (startBtn != null && startBtn.gameObject.activeInHierarchy && startBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Auto-start: clicking START button."); lastStartGameClickedTime = DateTime.Now; if (firstStartGameClickTime == DateTime.MinValue) { firstStartGameClickTime = DateTime.Now; } startBtn.onClick.Invoke(); return; } } // Runtime watchdog: if Start Game has been clicked repeatedly but the scene never // transitioned into a flight level (race never actually loaded — the real-world // "Drawing Board" failure mode), treat this track as a runtime failure: blacklist it // for the rest of this session and recover to a known-good state. This is // defense-in-depth alongside the pre-launch validation, since a track can pass every // static check and still fail only when the race actually starts. if (firstStartGameClickTime != DateTime.MinValue) { double sinceFirstClick = (DateTime.Now - firstStartGameClickTime).TotalSeconds; if (sinceFirstClick > RaceLoadTimeoutSeconds) { string failKey = $"{targetEnvironment}|{targetTrackName}|{targetGameMode}"; UnityEngine.Debug.LogError($"[AutoLobbyPlugin] RUNTIME FAILURE: race did not load {sinceFirstClick:F0}s after Start Game click for \'{targetTrackName}\' (Env: {targetEnvironment}, Mode: {targetGameMode}). Blacklisting for this session."); sessionBlacklistedTracks.Add(failKey); // Structured JSON file event (A3): an autonomous plugin decision — a track that // failed to load at runtime is blacklisted for the rest of the session. LogJsonEvent("decision", ("kind", "track_blacklist"), ("detail", $"{targetEnvironment} - {targetTrackName} ({targetGameMode}) failed to load {sinceFirstClick:F0}s after Start Game")); firstStartGameClickTime = DateTime.MinValue; NavigateToMainMenu(); return; } } // Warn about the next track 10 seconds before rotation if (elapsed >= GetRotationInterval() - 15.0 && elapsed < GetRotationInterval()) { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Close to rotation. chatWarned={chatWarnedAboutNextRace}, elapsed={elapsed:F1}s, target={GetRotationInterval() - 10.0:F1}s"); } } if (!chatWarnedAboutNextRace && elapsed >= GetRotationInterval() - 10.0 && elapsed < GetRotationInterval()) { chatWarnedAboutNextRace = true; string nextEnv, nextMode; int trackIdx; string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx); if (!string.IsNullOrEmpty(nextTrackName)) { SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} Up next: {FormatHighlight($"{nextEnv} - {nextTrackName}")}"); } else { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] PeekNextTrackName returned null/empty."); } } if (skipRequested || elapsed >= GetRotationInterval()) { if (skipRequested) UnityEngine.Debug.Log("[AutoLobbyPlugin] Skip requested by admin — forcing rotation."); skipRequested = false; chatWarnedAboutNextRace = false; // Timer expired (or skip forced) inside waiting room, open change settings popup! string[] changeSettingsNames = { "buttonChangeRoomSettings", "btnChangeRoomSettings", "ChangeRoomSettings" }; Button changeSettingsBtn = FindButtonByTextOrName("CHANGE SETTINGS", changeSettingsNames); if (changeSettingsBtn != null && changeSettingsBtn.gameObject.activeInHierarchy && changeSettingsBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Clicking CHANGE SETTINGS button."); changeSettingsBtn.onClick.Invoke(); roomCreatedTime = DateTime.MaxValue; // Freeze timer until settings updated } return; } HandleKeepAlive(); }',
    'HandleMainMenu': 'private static void HandleMainMenu() { // Gate (plugin-mode-split.md): no menu automation / auto sign-in in client mode. The // RunTick client branch already prevents reaching this; this guard is defense-in-depth // so the method is safe against any future caller. if (IsClientMode) return; // Reset rotation state roomCreatedTime = DateTime.MinValue; isLeaving = false; double timeSinceLoad = (DateTime.Now - sceneLoadTime).TotalSeconds; // Log all visible buttons every 5s so we can see what\'s on screen LogMultiplayerMenuState(); // Wait 3s for the menu to fully render before doing anything if (timeSinceLoad < 3.0) return; // Step 1: Sign in with Liftoff Pro if we haven\'t yet this session bool hasDistinctCredentials = !string.IsNullOrEmpty(liftoffProUsername) && !string.IsNullOrEmpty(liftoffProPassword); if (!liftoffProLoginAttempted && (!useLiftoffPro || hasDistinctCredentials)) { // use_liftoff_pro.txt=false, or distinct liftoff_pro_username/password.txt configured: // never click the MainMenu Pro sign-in button, since that would auto-login using // whatever account is already saved to this shared install\'s Credentials.xml // (production\'s own account) rather than the account we actually want this // instance to use. Falls through to MultiplayerMenu\'s sign-in screen instead, // where the credentialed or anonymous path takes over. UnityEngine.Debug.Log("[AutoLobbyPlugin] Skipping default Liftoff Pro sign-in on MainMenu (useLiftoffPro=false or distinct credentials configured)."); liftoffProLoginAttempted = true; } else if (!liftoffProLoginAttempted) { Button proBtn = FindLiftoffProSignInButton(); if (proBtn != null) { liftoffProLoginAttempted = true; liftoffProLoginClickTime = DateTime.Now; UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking Liftoff Pro sign-in button on MainMenu: name=\'{proBtn.name}\' text=\'{GetButtonText(proBtn)}\'"); proBtn.onClick.Invoke(); return; } else { // No Liftoff Pro button found — already signed in, or button not present if (DateTime.Now.Second % 10 == 0) UnityEngine.Debug.Log("[AutoLobbyPlugin] No Liftoff Pro sign-in button found on MainMenu — proceeding as already signed in."); liftoffProLoginAttempted = true; // don\'t keep searching every tick } } // Step 2: If we just clicked the Pro sign-in button, wait up to 30s for it to complete if (liftoffProLoginAttempted && liftoffProLoginClickTime != DateTime.MinValue) { double elapsed = (DateTime.Now - liftoffProLoginClickTime).TotalSeconds; // Check if the button disappeared (sign-in completed / we moved past that state) Button proBtn = FindLiftoffProSignInButton(); if (proBtn != null && elapsed < 30.0) { if (DateTime.Now.Second % 5 == 0) UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for Liftoff Pro sign-in to complete ({elapsed:F0}s / 30s)..."); return; } // Button gone or timeout reached — proceed liftoffProLoginClickTime = DateTime.MinValue; if (elapsed >= 30.0) UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Liftoff Pro sign-in timed out after 30s — proceeding anyway."); else UnityEngine.Debug.Log("[AutoLobbyPlugin] Liftoff Pro sign-in button is gone — sign-in likely completed."); } // Step 3: Navigate to Multiplayer — wait 5s total before navigating if (timeSinceLoad < 5.0) return; // 3a. Click the Lobby sub-button if already expanded string[] lobbyNames = { "MultiplayerLobby", "btnMultiplayerLobby" }; Button lobbyBtn = FindButtonByTextOrName("LOBBY", lobbyNames); if (lobbyBtn != null && lobbyBtn.gameObject.activeInHierarchy && lobbyBtn.interactable) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking LOBBY button: {lobbyBtn.name}"); lobbyBtn.onClick.Invoke(); return; } // 3b. Expand the Multiplayer category first string[] categoryNames = { "BtnHeading", "Multiplayer" }; Button categoryBtn = FindButtonByTextOrName("MULTIPLAYER", categoryNames); if (categoryBtn != null) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking MULTIPLAYER category button: {categoryBtn.name}"); categoryBtn.onClick.Invoke(); } }',
    'HandleMultiplayerMenu': 'private static void HandleMultiplayerMenu() { // Gate (plugin-mode-split.md): no auto room creation, join-by-name, or sign-in // automation in client mode — the player drives their own menus. The RunTick client // branch already prevents reaching this; this guard is defense-in-depth. if (IsClientMode) return; // Known long-standing Liftoff quirk, confirmed live 2026-07-02 (affects both Pro // and anonymous sign-in): once "An authentication request is still pending. Cannot // connect." fires, every further click on this same MultiplayerMenu instance keeps // failing the same way — some auth-manager flag never clears itself. Confirmed via // reflection that Photon\'s own connection is healthy when this fires // (NetworkClientState=ConnectedToMasterServer, IsConnectedAndReady=true), so it\'s a // game-logic-level guard flag, not a Photon-level stuck connection. // // Tried and disproven: reloading only the MultiplayerMenu scene in place // (SceneManager.LoadScene("MultiplayerMenu")) does NOT clear it — looped 25+ times // live, every retry hit the identical error. That rules out scene-bound state and // confirms the stuck flag lives on a cross-scene-persistent object (SignInManager is // a LugusSingletonCrossScene<T>; PlatformProvider.Instance is a similar singleton), // which a same-scene reload never touches. Only the full MainMenu round-trip has // actually been confirmed to clear it. if (authPendingErrorDetected) { authPendingErrorDetected = false; UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Detected stuck \'authentication request still pending\' error — cycling back to MainMenu to clear it (known Liftoff quirk)."); liftoffProLoginAttempted = false; liftoffProLoginClickTime = DateTime.MinValue; lastSkipClickTime = DateTime.MinValue; lastSignInClickTime = DateTime.MinValue; lastCredentialSubmitTime = DateTime.MinValue; signInWasVisible = false; signInClickAttempted = false; NavigateToMainMenu(); return; } DumpActiveSceneObjects(); LogMultiplayerMenuState(); // Distinct Liftoff Pro credentials (liftoff_pro_username.txt/liftoff_pro_password.txt) // take priority over both the anonymous and default-credentialed paths below — this // is how a test-client instance gets a genuinely distinct Photon identity instead of // colliding with other instances sharing this Steam login (see field comment above). bool hasDistinctCredentials = !string.IsNullOrEmpty(liftoffProUsername) && !string.IsNullOrEmpty(liftoffProPassword); if (hasDistinctCredentials) { HandleDistinctLiftoffProCredentials(); return; } // use_liftoff_pro.txt=false: click through Skip/Guest/Anonymous instead of the // credentialed sign-in flow below. Checked first so it takes priority whenever // present — a useLiftoffPro=false instance should never fall into the sign-in // candidate picker further down. if (!useLiftoffPro) { Button skipBtn = FindSkipLiftoffProButton(); if (skipBtn != null) { // 15s, not the original 5s: the game appears to fire its own automatic // connection/auto-login attempt as soon as this screen loads (matches the // "waits 10s for auto-login first" behavior already documented for the // credentialed path), and clicking Connect while that\'s still in flight is // the likely cause of the "still pending" error seen live 2026-07-02 on // literally the first click, even on a freshly-restarted Steam client. double timeSinceLoadSkip = (DateTime.Now - sceneLoadTime).TotalSeconds; if (timeSinceLoadSkip < 15.0) { if (DateTime.Now.Second % 5 == 0) UnityEngine.Debug.Log($"[AutoLobbyPlugin] Skip/anonymous button detected, waiting for UI to settle ({timeSinceLoadSkip:F1}s)..."); return; } // Confirmed live 2026-07-02: re-clicking Connect while the first anonymous // auth request is still in flight gets rejected by the game with // "An authentication request is still pending. Cannot connect." — 10s // wasn\'t enough. Matches the 30s cooldown the credentialed-recovery path // below already uses for the same reason ("auth takes time to process // server-side"). if ((DateTime.Now - lastSkipClickTime).TotalSeconds > 30.0) { lastSkipClickTime = DateTime.Now; UnityEngine.Debug.Log($"[AutoLobbyPlugin] useLiftoffPro=false — clicking skip/anonymous button: name=\'{skipBtn.name}\' text=\'{GetButtonText(skipBtn)}\'"); skipBtn.onClick.Invoke(); } return; } } // Self-correction: if the join-by-name sub-panel is active but we\'re not actually // driving a join-by-name flow right now, it\'s a leftover from an aborted flow (e.g. a // Photon disconnect mid-flow surfaced a sign-in screen and stranded this panel // underneath it — the reproduced 2026-07-02 incident). Reload the scene to force back // to the canonical lobby-list state instead of guessing at a "Back"/"Cancel" button // name for a panel whose real names have already fooled a decompiled-class guess once // (buttonJoinRoomByName vs. the real buttonJoinByName) — a fresh scene load destroys // the leftover panel outright and is safe here since nothing legitimate is in flight. bool expectedJoinByNameFlow = pendingJoinByName && !joinByNamePanelSubmitted; if (!expectedJoinByNameFlow) { InputField leftoverJoinField = FindInputFieldByName(JoinByNameRoomFieldNames, "game name"); if (leftoverJoinField != null && leftoverJoinField.gameObject.activeInHierarchy) { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Found leftover join-by-name panel active with no join-by-name flow in progress — reloading MultiplayerMenu to recover."); SceneManager.LoadScene("MultiplayerMenu"); return; } } // If a sign-in screen is still showing here (Liftoff Pro didn\'t complete from MainMenu), // log it prominently and navigate back to MainMenu to retry sign-in there. bool signInVisible = false; foreach (Button btn in Resources.FindObjectsOfTypeAll<Button>()) { if (btn == null || !btn.gameObject.activeInHierarchy) continue; string txt = GetButtonText(btn); if (string.IsNullOrEmpty(txt)) continue; bool isSignIn = txt.IndexOf("sign in", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("log in", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("login", StringComparison.OrdinalIgnoreCase) >= 0; bool isSkip = txt.IndexOf("skip", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("guest", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("anonymous", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("without", StringComparison.OrdinalIgnoreCase) >= 0; if (isSignIn && !isSkip) { signInVisible = true; UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Sign-in button still visible in MultiplayerMenu: name=\'{btn.name}\' text=\'{txt}\'"); } } if (signInVisible) { if (!signInWasVisible) { signInWasVisible = true; signInClickAttempted = false; } double timeSinceLoad = (DateTime.Now - sceneLoadTime).TotalSeconds; // Wait 5s for the UI to fully settle before clicking anything if (timeSinceLoad < 5.0) { if (DateTime.Now.Second % 5 == 0) UnityEngine.Debug.Log($"[AutoLobbyPlugin] Sign-in screen detected, waiting for UI to settle ({timeSinceLoad:F1}s)..."); return; } // Collect all sign-in button candidates with their screen positions var candidates = new List<Button>(); foreach (Button btn in Resources.FindObjectsOfTypeAll<Button>()) { if (btn == null || !btn.gameObject.activeInHierarchy || !btn.interactable) continue; string name = btn.name ?? ""; string txt = GetButtonText(btn); // Match by known button name first (most reliable) bool isSignInByName = name.Equals("buttonSignInCredentials", StringComparison.OrdinalIgnoreCase) || name.Equals("btnSignInCredentials", StringComparison.OrdinalIgnoreCase) || name.IndexOf("SignInCredentials", StringComparison.OrdinalIgnoreCase) >= 0; // Fallback: match by button text bool isSignInByText = !string.IsNullOrEmpty(txt) && ( txt.IndexOf("sign in", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("log in", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("login", StringComparison.OrdinalIgnoreCase) >= 0); bool isSkip = txt.IndexOf("skip", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("guest", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("anonymous", StringComparison.OrdinalIgnoreCase) >= 0 || txt.IndexOf("without", StringComparison.OrdinalIgnoreCase) >= 0; if ((isSignInByName || isSignInByText) && !isSkip) candidates.Add(btn); } // Log all candidates with positions so we can verify we pick the right one UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found {candidates.Count} sign-in button candidate(s). Screen size: {Screen.width}x{Screen.height}"); foreach (Button c in candidates) UnityEngine.Debug.Log($"[AutoLobbyPlugin] Candidate: name=\'{c.name}\' text=\'{GetButtonText(c)}\' screenPos={c.transform.position}"); if (candidates.Count == 0) { // sign-in was detected via text scan but now no candidates — UI might be transitioning return; } // Pick the button closest to vertical CENTER of screen (not the top nav bar button) float centerY = Screen.height / 2.0f; Button bestBtn = candidates[0]; float bestDist = Mathf.Abs(candidates[0].transform.position.y - centerY); foreach (Button c in candidates) { float dist = Mathf.Abs(c.transform.position.y - centerY); if (dist < bestDist) { bestDist = dist; bestBtn = c; } } // Exactly one click per sign-in-screen appearance (reduce-login-retry-attempts) — // retrying every 30s just delayed noticing a failed attempt, since the give-up // threshold below is now shorter than a second click\'s cooldown would allow anyway. if (!signInClickAttempted) { signInClickAttempted = true; lastSignInClickTime = DateTime.Now; UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking center sign-in button (single attempt): name=\'{bestBtn.name}\' text=\'{GetButtonText(bestBtn)}\' pos={bestBtn.transform.position}"); bestBtn.onClick.Invoke(); } else if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for sign-in response after single attempt ({(DateTime.Now - lastSignInClickTime).TotalSeconds:F0}s / 35s)..."); } // After 35s with no progress (just past the server-side ~30s auth window), go back // to MainMenu to reset state rather than waiting out the old 60s cap. if (timeSinceLoad > 35.0) { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Still on sign-in screen after 35s (single attempt exhausted) — returning to MainMenu."); liftoffProLoginAttempted = false; liftoffProLoginClickTime = DateTime.MinValue; signInClickAttempted = false; NavigateToMainMenu(); } return; } signInWasVisible = false; // 3. Check if GameRoom is active GameObject gameRoomObj = GameObject.Find("GameRoom"); bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy); if (inRoom) { lastInRoomTime = DateTime.Now; HandleGameRoom(); return; } // Not in room — check grace period before doing anything. // GameRoom can temporarily disappear during settings updates and Photon state syncs. // If we were in a room within the last 120s, hold position — do NOT create a new lobby. // Skipped entirely during a /private <name> rename: we left the room on purpose, so // there\'s nothing transient to wait out — go straight to the join-by-name/create logic // below. Without this, the grace period silently ate up to 120s doing nothing, and then // the stuck-in-menu fallback below could fire immediately afterwards (since sceneLoadTime // predates the leave), bouncing the bot out to MainMenu before it ever tried to recover. double timeInMenu = (DateTime.Now - sceneLoadTime).TotalSeconds; double timeSinceRoom = lastInRoomTime != DateTime.MinValue ? (DateTime.Now - lastInRoomTime).TotalSeconds : timeInMenu; if (!pendingPrivateRoomRename) { if (lastInRoomTime != DateTime.MinValue && timeSinceRoom < 120.0) { if (DateTime.Now.Second % 10 == 0) { bool photonConnected = GetPhotonBoolProperty("IsConnected"); bool photonInRoom = GetPhotonBoolProperty("InRoom"); bool photonReady = GetPhotonBoolProperty("IsConnectedAndReady"); UnityEngine.Debug.Log($"[AutoLobbyPlugin] GameRoom not found but was in room {timeSinceRoom:F0}s ago (grace period 120s). Photon Status: IsConnected={photonConnected}, InRoom={photonInRoom}, IsConnectedAndReady={photonReady}"); } return; } // Stuck-in-menu fallback: only fires after grace period if (timeInMenu > 90.0 && timeSinceRoom > 120.0) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Stuck in MultiplayerMenu for {timeInMenu:F0}s (out of room for {timeSinceRoom:F0}s) — navigating back to MainMenu."); NavigateToMainMenu(); return; } } // Grace period expired (or never been in a room this scene load) — reset state roomCreatedTime = DateTime.MinValue; isLeaving = false; // 3b. If a /private <name> request hit a name collision, drive the join-by-name UI instead of Create Game if (pendingJoinByName && !joinByNamePanelSubmitted) { ProcessJoinByNameFlow(); return; } // 4. Lobby (List of games): If we are on the Lobby screen, click Create Game string[] createNames = { "buttonCreateGame", "btnCreateGame", "CreateGame" }; Button createBtn = FindButtonByTextOrName("CREATE GAME", createNames); if (createBtn == null) { // Try text containing "create game" or "create" Button[] buttons = Resources.FindObjectsOfTypeAll<Button>(); foreach (var b in buttons) { if (b == null) continue; string txt = GetButtonText(b); if (txt.IndexOf("create game", StringComparison.OrdinalIgnoreCase) >= 0) { createBtn = b; break; } } } if (createBtn != null && createBtn.gameObject.activeInHierarchy) { bool isReady = IsMultiplayerClientReady(); if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Create button found. Interactable: {createBtn.interactable}, ClientReady: {isReady}"); } if (createBtn.interactable && isReady) { try { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking Create Game button: {createBtn.name}"); createBtn.onClick.Invoke(); } catch (Exception ex) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Clicking Create Game button failed: {ex.Message}"); } } return; } }',
    'InitializeRoleAndSettings': '// Resolved once, from Awake, using the plugin\'s BepInEx ConfigFile. Defaults to `server` // so an install with no config file present behaves exactly like today\'s bot. private static void InitializeRoleAndSettings(ConfigFile config) { var roleEntry = config.Bind( "General", "Role", "server", "Plugin role. \'server\' = the dedicated auto-lobby bot (default; orchestrator-driven; " + "behaviorally unchanged). \'client\' = runs inside a real player\'s own Liftoff: no menu " + "automation, no auto-login, no room creation, no nickname override, never quits the " + "game. Sits idle until invoked in a room the player hosts."); string roleStr = (roleEntry.Value ?? "server").Trim(); pluginRole = roleStr.Equals("client", StringComparison.OrdinalIgnoreCase) ? PluginRole.Client : PluginRole.Server; settingsSource = IsClientMode ? (ISettingsSource)new ConfigSettingsSource(config) : new FileSettingsSource(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Role resolved to: {pluginRole} (config \'General.Role\' = \'{roleStr}\'). " + $"Settings source: {settingsSource.GetType().Name}."); }',
    'IsAdmin': '// Server mode: admins come from admin_ids.txt (the operator\'s list). Client mode: the // installing player is implicitly the sole admin — there is no text file to hand-edit — // so "admin" means "this is the local player" (plugin-mode-split.md, "Client identity"). private static bool IsAdmin(string userId) { if (IsClientMode) return IsLocalPlayer(userId); return adminIds.Contains(userId); }',
    'IsClientMode': 'private static bool IsClientMode => pluginRole == PluginRole.Client;',
    'IsLocalPlayer': "// True when the given Photon chat user id belongs to the local player. Matches primarily // on the local Photon UserId — the same value space as the incoming chat id in // ChatMessagePatch — with the local SteamID as a secondary check. Both resolve lazily and // are cached. NOTE (needs live verification): whether Liftoff's chat userId equals the // Photon LocalPlayer.UserId (and/or the SteamID string) must be confirmed in-game before // client-mode admin can be considered proven. private static bool IsLocalPlayer(string userId) { if (string.IsNullOrEmpty(userId)) return false; string photonId = GetLocalPhotonUserId(); if (!string.IsNullOrEmpty(photonId) && string.Equals(userId, photonId, StringComparison.OrdinalIgnoreCase)) return true; if (!string.IsNullOrEmpty(localSteamId) && string.Equals(userId, localSteamId, StringComparison.OrdinalIgnoreCase)) return true; return false; }",
    'IsServerMode': 'private static bool IsServerMode => pluginRole == PluginRole.Server;',
    'NoteTickFailure': '// Failure containment for client mode (plugin-mode-split.md). On a player\'s machine there // is no watchdog, so a plugin that throws every tick is worse than one that does nothing: // after repeated consecutive failures, latch off for the session and say so once. Server // mode is unaffected — it keeps ticking exactly as before (its watchdog handles recovery), // so this changes nothing for the server role. private static void NoteTickFailure(Exception ex, string where) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in {where}: {ex}"); if (!IsClientMode) return; consecutiveTickFailures++; if (!clientHardDisabled && consecutiveTickFailures >= ClientTickFailureLimit) { clientHardDisabled = true; UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Client mode: {consecutiveTickFailures} consecutive tick failures — self-disabling for this session to protect the player\'s game."); try { SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} The auto-lobby plugin hit repeated errors and has disabled itself for this session."); } catch { /* best-effort; never throw from the failure path */ } } }',
    'OnWillRenderCanvases': 'private static void OnWillRenderCanvases() { try { // Run our tick logic every 1.0 seconds to keep overhead low if ((DateTime.Now - lastTickTime).TotalSeconds < 1.0) return; lastTickTime = DateTime.Now; RunTick(); consecutiveTickFailures = 0; // reached on any normal completion (incl. early returns) } catch (Exception ex) { // Catch all to ensure we never crash Unity\'s canvas rendering loop NoteTickFailure(ex, "OnWillRenderCanvases"); } }',
    'PhotonContainerPrefix': 'private static bool PhotonContainerPrefix(object __instance, MethodBase __originalMethod, object[] __args) { try { string methodName = __originalMethod.Name; if (methodName == "OnLeftRoom" || methodName == "OnDisconnected") { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Photon Callback: {methodName} detected. Immediately resetting lastInRoomTime to trigger lobby recovery."); lastInRoomTime = DateTime.MinValue; roomCreatedTime = DateTime.MinValue; isLeaving = false; } else if (methodName == "OnCreateRoomFailed" && __args != null && __args.Length >= 2) { // Not gated on pendingPrivateRoomRename: any create attempt (bot startup, // post-disconnect recreate, etc.) can hit a stale/occupied room name, not just // an explicit /private <name> request — always try to recover. HandleCreateRoomFailed((short)__args[0], __args[1] as string); } else if (methodName == "OnJoinRoomFailed" && joinByNamePanelSubmitted && __args != null && __args.Length >= 2) { HandleJoinByNameFailed((short)__args[0], __args[1] as string); } else if (methodName == "OnCreatedRoom") { roomOwnedByBot = true; // democracy-skip.md: a freshly created room starts with no skip votes. skipVotes.Clear(); if (pendingPrivateRoomRename) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Private room rename: new room created successfully."); QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room recreated as private. Join name: {FormatVariable($"{pendingPrivateRoomName}")}."); } pendingPrivateRoomRename = false; pendingPrivateRoomRenameStartTime = DateTime.MinValue; pendingJoinByName = false; joinByNamePanelSubmitted = false; } else if (methodName == "OnJoinedRoom" && joinByNamePanelSubmitted) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Joined an existing room by name instead of creating one — bot does not own this room."); roomOwnedByBot = false; // democracy-skip.md: entering a (different) room starts with no skip votes. skipVotes.Clear(); QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} A room named \'{FormatVariable($"{pendingPrivateRoomName}")}\' already existed — joined it instead of creating a new one. <color={activeTheme.alertTagColor}><i>This bot is not the room owner and cannot control settings/rotation here.</i></color> Current host: please transfer host to this bot from the player list so it can control settings/rotation, or use /private with a different name to have the bot create its own room instead."); pendingPrivateRoomRename = false; pendingPrivateRoomRenameStartTime = DateTime.MinValue; pendingJoinByName = false; joinByNamePanelSubmitted = false; } else if (methodName == "OnMasterClientSwitched" && __args != null && __args.Length >= 1) { HandleMasterClientSwitched(__args[0]); } else if (methodName == "OnPlayerEnteredRoom" && __args != null && __args.Length >= 1) { LogPlayerPresenceEvent("player_join", __args[0]); } else if (methodName == "OnPlayerLeftRoom" && __args != null && __args.Length >= 1) { LogPlayerPresenceEvent("player_leave", __args[0]); } System.Collections.IList list = __instance as System.Collections.IList; if (list == null) return true; // Copy targets to avoid collection modified exceptions object[] targets; lock (list) { targets = new object[list.Count]; list.CopyTo(targets, 0); } // Find the interface type that defines this callback Type interfaceType = null; foreach (var iface in __instance.GetType().GetInterfaces()) { if (iface.Name.EndsWith("Callbacks") || iface.Name.Contains("Callback")) { interfaceType = iface; break; } } if (interfaceType == null) return true; // Resolve the interface method matching name and parameter types var paramTypes = __originalMethod.GetParameters().Select(p => p.ParameterType).ToArray(); MethodInfo interfaceMethod = interfaceType.GetMethod(__originalMethod.Name, paramTypes); if (interfaceMethod == null) return true; foreach (var callback in targets) { if (callback == null) continue; try { interfaceMethod.Invoke(callback, __args); } catch (Exception ex) { // Log the actual underlying exception Exception realEx = ex.InnerException ?? ex; UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Suppressed exception in {interfaceType.Name} listener ({callback.GetType().FullName}): {realEx}"); } } return false; // Skip the original looping method which would abort on exception } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PhotonContainerPrefix: {ex}"); return true; // Fallback to original method on error } }',
    'PluginRole': "// ───────────────────────────────────────────────────────────────────────────── // Plugin role + settings source (plugin-mode-split.md, R3 of public-release-v1) // // One compiled DLL, two roles. `role = server` (default) is exactly today's // orchestrator-driven bot; `role = client` runs inside a real player's own game and // must never automate menus, auto-login, create rooms, rename the player, or quit the // game (see the gate table in the feature doc). // // Settings come from exactly ONE source per role — never a merge or a sync step // (AGENTS.md rule 4). Server reads the orchestrator's plain-text protocol files in // BepInEx/plugins/; client reads its own BepInEx ConfigFile (player-editable, visible // in ConfigurationManager). The role picks exactly one ISettingsSource; shared code // reads settings only through it. // ───────────────────────────────────────────────────────────────────────────── internal enum PluginRole { Server, Client }",
    'RunServerMaintenanceTick': '// Server-only maintenance tick: external-trigger detection + scheduled-shutdown warnings // and the final Application.Quit(). Extracted verbatim from RunTick so it can be gated to // server mode without touching its logic. Returns true when the game is quitting (the // caller must then stop the rest of the tick), matching the original `return`. private static bool RunServerMaintenanceTick() { // 1. Check for external/internal maintenance mode try { string mPath = Path.Combine(pluginPath, "maintenance_active.txt"); if (File.Exists(mPath)) { if (!maintenanceActive) { maintenanceActive = true; maintenanceTime = DateTime.Now.AddMinutes(3.0); lastMaintenanceWarningMinutes = -1; maintenanceWarning30sSent = false; maintenanceWarning10sSent = false; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance scheduled in {FormatVariable($"3.0m")} (triggered externally)."); UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode triggered externally."); } } else { if (maintenanceActive) { CancelMaintenance(); SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Scheduled maintenance cancelled externally."); UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode cancelled externally."); } } } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error checking external maintenance file: {ex.Message}"); } if (maintenanceActive) { double remainingSecs = (maintenanceTime - DateTime.Now).TotalSeconds; if (remainingSecs <= 0) { SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Going down for maintenance."); UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance time reached. Exiting game."); Application.Quit(); return true; // Prevent running other tick logic } else { int remainingMinutes = (int)Math.Ceiling(remainingSecs / 60.0); if (remainingMinutes > 0 && remainingMinutes != lastMaintenanceWarningMinutes && remainingSecs > 30.0) { lastMaintenanceWarningMinutes = remainingMinutes; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"{remainingMinutes}m")}."); } else if (remainingSecs <= 30.0 && !maintenanceWarning30sSent) { maintenanceWarning30sSent = true; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"30s")}."); } else if (remainingSecs <= 10.0 && !maintenanceWarning10sSent) { maintenanceWarning10sSent = true; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"10s")}."); } } } return false; }',
    'RunTick': 'private static void RunTick() { // Failure containment (plugin-mode-split.md): if client mode has hit repeated // uncaught tick errors it self-disables for the session rather than thrashing the // player\'s game. Server mode never sets this flag (it has a watchdog). if (clientHardDisabled) return; ApplyBotNicknameIfNeeded(); // Maintenance mode is server-only: a player\'s game must never be closed from under // them (DANGER gate). The whole block — external trigger, warnings, and the final // Application.Quit() — runs only in server mode. if (IsServerMode && RunServerMaintenanceTick()) return; if (!steamStatusLogged) { steamStatusLogged = true; try { bool isRunning = Steamworks.SteamAPI.IsSteamRunning(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] SteamAPI.IsSteamRunning(): {isRunning}"); try { if (Steamworks.SteamAPI.Init()) { UnityEngine.Debug.Log("[AutoLobbyPlugin] SteamAPI.Init() returned True."); string personaName = Steamworks.SteamFriends.GetPersonaName(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Steam Persona Name: {personaName}"); ulong steamId = (ulong)Steamworks.SteamUser.GetSteamID(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Steam ID: {steamId}"); // Captured for client-mode admin resolution (IsLocalPlayer): in client // mode the installing player — this Steam account — is implicitly admin. if (steamId != 0) localSteamId = steamId.ToString(); } else { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] SteamAPI.Init() returned False."); } } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception when calling SteamAPI.Init(): {ex.Message}"); } } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to check Steam status: {ex.Message}"); } } string sceneName = SceneManager.GetActiveScene().name; if (sceneName != lastSceneName) { string previousSceneName = lastSceneName; lastSceneName = sceneName; sceneLoadTime = DateTime.Now; lastInRoomTime = DateTime.MinValue; sceneObjectsDumped = false; lastMenuStateDumpTime = DateTime.MinValue; // democracy-skip.md: any scene change invalidates in-flight skip votes. skipVotes.Clear(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Scene changed to: {sceneName}"); LogEvent("scene_change", ("scene", sceneName)); // Structured JSON file event (A3): from/to per the canonical schema. The very // first transition has no meaningful prior scene, so "from" is omitted (null). LogJsonEvent("scene_change", ("from", string.IsNullOrEmpty(previousSceneName) ? null : previousSceneName), ("to", sceneName)); // Reset room timer when loading into a flight level scene if (sceneName != "MainMenu" && sceneName != "MultiplayerMenu" && sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene") { UnityEngine.Debug.Log("[AutoLobbyPlugin] Level loaded. Resetting room timer."); roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; isLeaving = false; firstStartGameClickTime = DateTime.MinValue; // race loaded successfully, disarm runtime watchdog } } // Log status every 30 seconds for visibility if (DateTime.Now.Second % 30 == 0) { double elapsed = roomCreatedTime != DateTime.MinValue ? (DateTime.Now - roomCreatedTime).TotalSeconds : 0; UnityEngine.Debug.Log($"[AutoLobbyPlugin] Tick running. Scene: {sceneName}, Room timer elapsed: {elapsed:F1}s / {GetRotationInterval()}s"); } // ── Client-mode gate (plugin-mode-split.md, R3) ──────────────────────────────── // Everything below this point is autonomous UI automation that only ever runs in // server mode: the private-room rename recovery net, popup dismissal, the settings- // popup create/update flow (ConfigureAndCreateRoom), and the MainMenu / MultiplayerMenu // / FlightLevel scene handlers. Client mode must never automate menus, sign in, create // rooms, or exit a flight level the player is in — it sits idle in the player\'s game // until the player engages rotation via a lifecycle command (client-lifecycle-commands // .md, R4). The shared HandleGameRoom / HandleFlightLevel / settings-Update path is // driven from HandleClientTick once engaged. This single branch is what gates the whole // server-only tail; in server mode IsClientMode is always false, so it is inert. if (IsClientMode) { HandleClientTick(sceneName); return; } // Global safety net: the join-by-name flow\'s own internal timeouts (15s hard cap, // 10s field-lookup cap) only fire from inside ProcessJoinByNameFlow/HandleCreateRoomFailed // — if a /private <name> rename gets derailed onto an unrelated screen before ever // reaching a create/join attempt (e.g. leaving a room triggers a full Photon disconnect // that surfaces a sign-in prompt), none of those internal timeouts ever run, and the bot // can wander indefinitely. This fires regardless of which scene/panel it\'s stuck on. if (pendingPrivateRoomRename && pendingPrivateRoomRenameStartTime != DateTime.MinValue && (DateTime.Now - pendingPrivateRoomRenameStartTime).TotalSeconds > 90.0) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Private room rename to \'{pendingPrivateRoomName}\' stuck for 90s+ with no create/join resolution — aborting and reloading MainMenu to recover."); pendingPrivateRoomRename = false; pendingPrivateRoomRenameStartTime = DateTime.MinValue; pendingJoinByName = false; joinByNamePanelSubmitted = false; liftoffProLoginAttempted = false; liftoffProLoginClickTime = DateTime.MinValue; try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "false"); } catch { } QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room rename to \'{FormatVariable($"{pendingPrivateRoomName}")}\' got stuck and was aborted — recovering with a public room."); SceneManager.LoadScene("MainMenu"); return; } if ((DateTime.Now - popupSubmittedTime).TotalSeconds < 5.0) { return; } DismissPopups(); // Check if settings popup is open globally (in MultiplayerMenu or Flight Level) PopupQuickPlayMultiplayerSetup popup = GameObject.FindObjectOfType<PopupQuickPlayMultiplayerSetup>(); bool popupOpen = (popup != null && popup.gameObject.activeInHierarchy); if (popupOpen) { if (!popupWasOpen) { popupOpenedTime = DateTime.Now; isSubmittingSettings = false; triedCustomContentTab = false; // Read config files on popup open targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode); string lobbyNamePath = Path.Combine(pluginPath, "lobby_name.txt"); if (File.Exists(lobbyNamePath)) { targetLobbyName = File.ReadAllText(lobbyNamePath).Trim(); } if (string.IsNullOrEmpty(targetLobbyName)) { targetLobbyName = "Procedural Loop Room"; } popupWasOpen = true; } // A create attempt just failed on a name collision — back out of this popup // instead of retrying Create with the same (still-taken) name, so the bot can // reach the lobby-list screen and drive the join-by-name fallback from there. if (pendingJoinByName && !joinByNamePanelSubmitted) { isSubmittingSettings = false; Button cancelBtn = GetPopupCancelButton(popup); if (cancelBtn != null && cancelBtn.gameObject.activeInHierarchy && cancelBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Cancelling settings popup to pivot to join-by-name fallback."); cancelBtn.onClick.Invoke(); } return; } // If settings are already submitted, do not touch the UI elements again if (isSubmittingSettings) { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Settings submitted, waiting for settings popup to close..."); } return; } // Wait 2 seconds for settings popup to fully initialize double popupAge = (DateTime.Now - popupOpenedTime).TotalSeconds; if (popupAge < 2.0) { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for settings popup to initialize (age: {popupAge:F1}s)..."); } return; } ConfigureAndCreateRoom(popup); return; } else { if (popupWasOpen) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Settings popup closed."); // If the room timer was frozen, unfreeze/reset it now that the popup is closed if (roomCreatedTime == DateTime.MaxValue) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Unfreezing room timer (setting to DateTime.Now)."); roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; chatWarnedAboutNextRace = false; } isSubmittingSettings = false; } popupWasOpen = false; } if (string.IsNullOrEmpty(sceneName)) return; if (sceneName == "MainMenu") { HandleMainMenu(); } else if (sceneName == "MultiplayerMenu") { HandleMultiplayerMenu(); } else if (sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene") { popupWasOpen = false; HandleFlightLevel(); } }',
    'Settings': 'private static ISettingsSource Settings => settingsSource;',
    'Update': 'private void Update() { try { // Run our tick logic every 1.0 seconds to keep overhead low if ((DateTime.Now - lastTickTime).TotalSeconds < 1.0) return; lastTickTime = DateTime.Now; RunTick(); consecutiveTickFailures = 0; // reached on any normal completion (incl. early returns) } catch (Exception ex) { NoteTickFailure(ex, "Update"); } }',
    'cachedLocalPhotonUserId': '// Reflects PhotonNetwork.LocalPlayer.UserId — the same value space as the chat user ids // delivered to ChatMessagePatch — so client-mode admin resolution (IsLocalPlayer) can // compare like-for-like. Cached once non-empty; empty until Photon has assigned a local // player (i.e. once connected/in a room). private static string cachedLocalPhotonUserId = "";',
    'clientHardDisabled': 'private static bool clientHardDisabled = false;',
    'clientRotationEngaged': '// Client-mode engagement + failure containment (plugin-mode-split.md, R3). // clientRotationEngaged: false at rest; client-lifecycle-commands.md (R4) flips it via // /start so the shared rotation loop begins driving a room the player already hosts. // At R3 it is never set, so client mode does nothing until invoked. // clientHardDisabled: latched after repeated uncaught tick failures in client mode, so a // broken plugin degrades to "does nothing" instead of thrashing the player\'s game. private static bool clientRotationEngaged = false;',
    'consecutiveTickFailures': 'private static int consecutiveTickFailures = 0;',
    'democracyEnabled': "// Democracy mode (democracy-skip.md): when enabled, /skip becomes a public majority // vote instead of admin-only. skipVotes holds the unique Photon User IDs of players // who have voted to skip the current track; cleared on new track load, scene change, // and room create/enter (see CaptureLoadedTrack, the scene-change block in // OnWillRenderCanvases/Update, and PhotonContainerPrefix's OnCreatedRoom/OnJoinedRoom). private static bool democracyEnabled = false;",
    'localSteamId': '// Local player identity, for client-mode admin resolution. localSteamId is captured from // the Steam init block in RunTick; the Photon UserId is resolved lazily (see Photon.cs). private static string localSteamId = "";',
    'pluginRole': 'private static PluginRole pluginRole = PluginRole.Server;',
    'private interface ISettingsSource': '// The config settings read from SHARED rotation/game-room paths, so they must resolve // in both roles once client rotation is engaged (client-lifecycle-commands.md, R4). // Server-automation-only settings (Liftoff Pro toggle/nickname/credentials) stay // file-only and are loaded only in server mode — client never touches those paths. private interface ISettingsSource { double RotationIntervalSeconds { get; } double KeepAliveSeconds { get; } bool AutoStart { get; } bool ShuffleMode { get; } bool DemocracyMode { get; } string OverrideGameMode { get; } // null when unset (matches the file default) }',
    'private sealed class ConfigSettingsSource : ISettingsSource': '// Client source: the plugin\'s own BepInEx ConfigFile — player-editable, visible in // ConfigurationManager. Defaults mirror the server file defaults so nothing behaves // surprisingly. // // NOTE (R4 boundary): chat commands that CHANGE these settings still write the server // text files today (e.g. ShuffleCommand -> shuffle_mode.txt). Routing those WRITES to // ConfigEntry.Value for client mode is client-lifecycle-commands.md\'s job. This is inert // at R3 because client rotation is never engaged, so the only client-mode read of these // is the Awake-time initialisation of the shuffle/democracy statics (which correctly // returns these defaults) — there is no live path that could observe a stale value. private sealed class ConfigSettingsSource : ISettingsSource { private readonly ConfigEntry<double> rotationInterval; private readonly ConfigEntry<double> keepAlive; private readonly ConfigEntry<bool> autoStart; private readonly ConfigEntry<bool> shuffle; private readonly ConfigEntry<bool> democracy; private readonly ConfigEntry<string> overrideGameMode; public ConfigSettingsSource(ConfigFile config) { rotationInterval = config.Bind("Rotation", "IntervalSeconds", 600.0, "Seconds each track stays in rotation before advancing."); keepAlive = config.Bind("Rotation", "KeepAliveSeconds", 240.0, "Seconds of room inactivity before the plugin posts a keep-alive chat line."); autoStart = config.Bind("Rotation", "AutoStart", false, "Automatically click START a few seconds after players are in the room."); shuffle = config.Bind("Rotation", "Shuffle", false, "Shuffle the rotation order each full pass instead of playing it in file order."); democracy = config.Bind("Commands", "Democracy", false, "Let players vote to skip a track with /skip instead of it being admin-only."); overrideGameMode = config.Bind("Rotation", "OverrideGameMode", "", "Force a single game mode for every track (blank = use each rotation line\'s own mode)."); } public double RotationIntervalSeconds => rotationInterval.Value; public double KeepAliveSeconds => keepAlive.Value; public bool AutoStart => autoStart.Value; public bool ShuffleMode => shuffle.Value; public bool DemocracyMode => democracy.Value; public string OverrideGameMode => string.IsNullOrEmpty(overrideGameMode.Value) ? null : overrideGameMode.Value; }',
    'private sealed class FileSettingsSource : ISettingsSource': '// Server source: the orchestrator\'s plain-text protocol files in BepInEx/plugins/, read // fresh on each access so the orchestrator can change them live — identical to the // original Get* readers this replaced. private sealed class FileSettingsSource : ISettingsSource { public double RotationIntervalSeconds { get { try { string p = Path.Combine(pluginPath, "rotation_interval.txt"); double v; if (File.Exists(p) && double.TryParse(File.ReadAllText(p).Trim(), out v)) return v; } catch { } return 600.0; // Default: 10 mins } } public double KeepAliveSeconds { get { try { string p = Path.Combine(pluginPath, "keep_alive_seconds.txt"); double v; if (File.Exists(p) && double.TryParse(File.ReadAllText(p).Trim(), out v)) return v; } catch { } return 240.0; // Default: 4 minutes (resets timer right before 5m kick) } } public bool AutoStart => FileFlag("auto_start.txt"); public bool ShuffleMode => FileFlag("shuffle_mode.txt"); public bool DemocracyMode => FileFlag("democracy_mode.txt"); public string OverrideGameMode { get { try { string p = Path.Combine(pluginPath, "override_game_mode.txt"); if (File.Exists(p)) { string mode = File.ReadAllText(p).Trim(); if (!string.IsNullOrEmpty(mode)) return mode; } } catch { } return null; } } private static bool FileFlag(string fileName) { try { string p = Path.Combine(pluginPath, fileName); if (File.Exists(p)) return File.ReadAllText(p).Trim().Equals("true", StringComparison.OrdinalIgnoreCase); } catch { } return false; } }',
    'settingsSource': "// Defaults to the file source so any code path that runs before InitializeRoleAndSettings // (there should be none) still behaves like today's server bot. private static ISettingsSource settingsSource = new FileSettingsSource();",
    'skipVotes': 'private static HashSet<string> skipVotes = new HashSet<string>(StringComparer.OrdinalIgnoreCase);',
}

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
        if name in PINNED_POST_SPLIT_BODIES:
            # Documented post-split integration change (democracy-skip) -- its
            # exact resulting body is checked against a pinned literal by
            # test_pinned_post_split_bodies_match_exactly instead of against
            # the pre-split snapshot, which necessarily predates it.
            continue
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
    extra = sorted(set(post_split_members) - set(pre_split_members) - set(PINNED_POST_SPLIT_BODIES))
    assert not extra, f"symbol(s) present after the split that didn't exist before it: {extra}"


def test_pinned_post_split_bodies_match_exactly(post_split_members):
    """Guards PINNED_POST_SPLIT_BODIES itself: the 3 symbols democracy-skip added
    and the 5 pre-split symbols it appended lines to must match their recorded
    post-merge body EXACTLY (whitespace-normalized). This is what keeps the two
    exemptions above from silently degrading into "anything goes" for these 8
    names -- any further, undocumented change to them fails here."""
    mismatches = []
    missing = []
    for name, expected_body in PINNED_POST_SPLIT_BODIES.items():
        occs = post_split_members.get(name)
        if not occs:
            missing.append(name)
            continue
        _, post_body = occs[0]
        if post_body != expected_body:
            mismatches.append(name)
    assert not missing, f"pinned post-split symbol(s) not found in any post-split file: {missing}"
    assert not mismatches, (
        f"pinned post-split symbol(s) drifted from their recorded integration body: {mismatches} "
        "-- if this is an intentional new change, update PINNED_POST_SPLIT_BODIES to match AND "
        "record why in the relevant feature doc; do not update this silently"
    )


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
        assert re.search(r"//\s*MODE:\s*(shared|server-only|mixed)", text), (
            f"{os.path.basename(path)} is missing the '// MODE: shared|server-only|mixed' header"
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
