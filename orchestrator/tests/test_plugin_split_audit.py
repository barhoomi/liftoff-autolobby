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


# Post-split integration deltas from feature/democracy-skip (merged into
# integration/decomp-democracy 2026-07-16 -- see
# docs/features/doing/democracy-skip.md, "Integration 2026-07-16" section).
#
# The frozen baseline snapshot captures Plugin.cs the instant BEFORE the split
# (052f888) -- it proves the split itself was pure code motion, but it was never
# meant to freeze Plugin.cs's *content* forever. Normal feature work continues to
# land on top of the decomposed layout exactly as it would have landed on the old
# monolith. democracy-skip added 3 brand-new symbols and appended lines to 5
# pre-existing pre-split symbols.
#
# Rather than blanket-exempting these 8 names from the audit (which would
# silently swallow ANY future edit to them, not just this one, documented change --
# exactly the kind of drift this audit exists to catch), their exact post-merge
# (whitespace-normalized) bodies are pinned here and checked verbatim by
# test_pinned_post_split_bodies_match_exactly below. A future, undocumented change
# to any of them will still fail this audit; an intentional one must update the
# pinned value here AND record why in the relevant feature doc.
PINNED_POST_SPLIT_BODIES = {
    'democracyEnabled': "// Democracy mode (democracy-skip.md): when enabled, /skip becomes a public majority // vote instead of admin-only. skipVotes holds the unique Photon User IDs of players // who have voted to skip the current track; cleared on new track load, scene change, // and room create/enter (see CaptureLoadedTrack, the scene-change block in // OnWillRenderCanvases/Update, and PhotonContainerPrefix's OnCreatedRoom/OnJoinedRoom). private static bool democracyEnabled = false;",
    'skipVotes': 'private static HashSet<string> skipVotes = new HashSet<string>(StringComparer.OrdinalIgnoreCase);',
    'GetDemocracyMode': '// democracy-skip.md: whether /skip is a public majority vote (true) or admin-only // (false). Mirrors GetShuffleMode()\'s file-read pattern exactly. private static bool GetDemocracyMode() { try { string democracyModePath = Path.Combine(pluginPath, "democracy_mode.txt"); if (File.Exists(democracyModePath)) { string content = File.ReadAllText(democracyModePath).Trim(); return content.Equals("true", StringComparison.OrdinalIgnoreCase); } } catch {} return false; // Default: democracy off, /skip stays admin-only }',
    'Awake': 'private void Awake() { Logger.LogInfo("[AutoLobbyPlugin] BepInEx Awake called!"); try { pluginPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "BepInEx", "plugins"); LoadThemeConfig(); LoadAdminIds(); LoadUseLiftoffPro(); LoadBotNickname(); LoadLiftoffProCredentials(); LoadClientScript(); // Load initial shuffle mode shuffleMode = GetShuffleMode(); Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial shuffleMode: {shuffleMode}"); // Load initial democracy mode (democracy-skip.md) democracyEnabled = GetDemocracyMode(); Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial democracyEnabled: {democracyEnabled}"); // Register all chat commands with the command registry (replaces the old // hardcoded HandleChatCommand switch). CommandRegistry.Initialize(); // Apply Harmony patches to fix database loading exceptions ApplyHarmonyPatches(); // Dynamically resolve multiplayer client connection check method ResolveMultiplayerClientCheck(); // Subscribe to the static Canvas render event (runs on main thread every frame) Canvas.willRenderCanvases += OnWillRenderCanvases; // Known long-standing Liftoff quirk (both Pro and anonymous sign-in): the // sign-in flow can get stuck reporting "An authentication request is still // pending. Cannot connect." on every further click, seemingly forever, until // the MultiplayerMenu scene is torn down and reloaded (previously worked // around by hand: back to MainMenu, back into Multiplayer). Hooking Unity\'s // log callback lets HandleMultiplayerMenu() detect this the moment it fires // and trigger that same recovery automatically instead of hanging. Application.logMessageReceived += OnUnityLogMessageReceived; Logger.LogInfo("[AutoLobbyPlugin] Static Canvas.willRenderCanvases hook registered successfully!"); } catch (Exception ex) { Logger.LogError($"[AutoLobbyPlugin] Static hook registration failed: {ex.Message}"); } }',
    'RunTick': 'private static void RunTick() { ApplyBotNicknameIfNeeded(); // 1. Check for external/internal maintenance mode try { string mPath = Path.Combine(pluginPath, "maintenance_active.txt"); if (File.Exists(mPath)) { if (!maintenanceActive) { maintenanceActive = true; maintenanceTime = DateTime.Now.AddMinutes(3.0); lastMaintenanceWarningMinutes = -1; maintenanceWarning30sSent = false; maintenanceWarning10sSent = false; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance scheduled in {FormatVariable($"3.0m")} (triggered externally)."); UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode triggered externally."); } } else { if (maintenanceActive) { CancelMaintenance(); SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Scheduled maintenance cancelled externally."); UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode cancelled externally."); } } } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error checking external maintenance file: {ex.Message}"); } if (maintenanceActive) { double remainingSecs = (maintenanceTime - DateTime.Now).TotalSeconds; if (remainingSecs <= 0) { SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Going down for maintenance."); UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance time reached. Exiting game."); Application.Quit(); return; // Prevent running other tick logic } else { int remainingMinutes = (int)Math.Ceiling(remainingSecs / 60.0); if (remainingMinutes > 0 && remainingMinutes != lastMaintenanceWarningMinutes && remainingSecs > 30.0) { lastMaintenanceWarningMinutes = remainingMinutes; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"{remainingMinutes}m")}."); } else if (remainingSecs <= 30.0 && !maintenanceWarning30sSent) { maintenanceWarning30sSent = true; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"30s")}."); } else if (remainingSecs <= 10.0 && !maintenanceWarning10sSent) { maintenanceWarning10sSent = true; SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"10s")}."); } } } if (!steamStatusLogged) { steamStatusLogged = true; try { bool isRunning = Steamworks.SteamAPI.IsSteamRunning(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] SteamAPI.IsSteamRunning(): {isRunning}"); try { if (Steamworks.SteamAPI.Init()) { UnityEngine.Debug.Log("[AutoLobbyPlugin] SteamAPI.Init() returned True."); string personaName = Steamworks.SteamFriends.GetPersonaName(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Steam Persona Name: {personaName}"); ulong steamId = (ulong)Steamworks.SteamUser.GetSteamID(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Steam ID: {steamId}"); } else { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] SteamAPI.Init() returned False."); } } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception when calling SteamAPI.Init(): {ex.Message}"); } } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to check Steam status: {ex.Message}"); } } string sceneName = SceneManager.GetActiveScene().name; if (sceneName != lastSceneName) { string previousSceneName = lastSceneName; lastSceneName = sceneName; sceneLoadTime = DateTime.Now; lastInRoomTime = DateTime.MinValue; sceneObjectsDumped = false; lastMenuStateDumpTime = DateTime.MinValue; // democracy-skip.md: any scene change invalidates in-flight skip votes. skipVotes.Clear(); UnityEngine.Debug.Log($"[AutoLobbyPlugin] Scene changed to: {sceneName}"); LogEvent("scene_change", ("scene", sceneName)); // Structured JSON file event (A3): from/to per the canonical schema. The very // first transition has no meaningful prior scene, so "from" is omitted (null). LogJsonEvent("scene_change", ("from", string.IsNullOrEmpty(previousSceneName) ? null : previousSceneName), ("to", sceneName)); // Reset room timer when loading into a flight level scene if (sceneName != "MainMenu" && sceneName != "MultiplayerMenu" && sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene") { UnityEngine.Debug.Log("[AutoLobbyPlugin] Level loaded. Resetting room timer."); roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; isLeaving = false; firstStartGameClickTime = DateTime.MinValue; // race loaded successfully, disarm runtime watchdog } } // Log status every 30 seconds for visibility if (DateTime.Now.Second % 30 == 0) { double elapsed = roomCreatedTime != DateTime.MinValue ? (DateTime.Now - roomCreatedTime).TotalSeconds : 0; UnityEngine.Debug.Log($"[AutoLobbyPlugin] Tick running. Scene: {sceneName}, Room timer elapsed: {elapsed:F1}s / {GetRotationInterval()}s"); } // Global safety net: the join-by-name flow\'s own internal timeouts (15s hard cap, // 10s field-lookup cap) only fire from inside ProcessJoinByNameFlow/HandleCreateRoomFailed // — if a /private <name> rename gets derailed onto an unrelated screen before ever // reaching a create/join attempt (e.g. leaving a room triggers a full Photon disconnect // that surfaces a sign-in prompt), none of those internal timeouts ever run, and the bot // can wander indefinitely. This fires regardless of which scene/panel it\'s stuck on. if (pendingPrivateRoomRename && pendingPrivateRoomRenameStartTime != DateTime.MinValue && (DateTime.Now - pendingPrivateRoomRenameStartTime).TotalSeconds > 90.0) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Private room rename to \'{pendingPrivateRoomName}\' stuck for 90s+ with no create/join resolution — aborting and reloading MainMenu to recover."); pendingPrivateRoomRename = false; pendingPrivateRoomRenameStartTime = DateTime.MinValue; pendingJoinByName = false; joinByNamePanelSubmitted = false; liftoffProLoginAttempted = false; liftoffProLoginClickTime = DateTime.MinValue; try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "false"); } catch { } QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room rename to \'{FormatVariable($"{pendingPrivateRoomName}")}\' got stuck and was aborted — recovering with a public room."); SceneManager.LoadScene("MainMenu"); return; } if ((DateTime.Now - popupSubmittedTime).TotalSeconds < 5.0) { return; } DismissPopups(); // Check if settings popup is open globally (in MultiplayerMenu or Flight Level) PopupQuickPlayMultiplayerSetup popup = GameObject.FindObjectOfType<PopupQuickPlayMultiplayerSetup>(); bool popupOpen = (popup != null && popup.gameObject.activeInHierarchy); if (popupOpen) { if (!popupWasOpen) { popupOpenedTime = DateTime.Now; isSubmittingSettings = false; triedCustomContentTab = false; // Read config files on popup open targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode); string lobbyNamePath = Path.Combine(pluginPath, "lobby_name.txt"); if (File.Exists(lobbyNamePath)) { targetLobbyName = File.ReadAllText(lobbyNamePath).Trim(); } if (string.IsNullOrEmpty(targetLobbyName)) { targetLobbyName = "Procedural Loop Room"; } popupWasOpen = true; } // A create attempt just failed on a name collision — back out of this popup // instead of retrying Create with the same (still-taken) name, so the bot can // reach the lobby-list screen and drive the join-by-name fallback from there. if (pendingJoinByName && !joinByNamePanelSubmitted) { isSubmittingSettings = false; Button cancelBtn = GetPopupCancelButton(popup); if (cancelBtn != null && cancelBtn.gameObject.activeInHierarchy && cancelBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Cancelling settings popup to pivot to join-by-name fallback."); cancelBtn.onClick.Invoke(); } return; } // If settings are already submitted, do not touch the UI elements again if (isSubmittingSettings) { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Settings submitted, waiting for settings popup to close..."); } return; } // Wait 2 seconds for settings popup to fully initialize double popupAge = (DateTime.Now - popupOpenedTime).TotalSeconds; if (popupAge < 2.0) { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for settings popup to initialize (age: {popupAge:F1}s)..."); } return; } ConfigureAndCreateRoom(popup); return; } else { if (popupWasOpen) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Settings popup closed."); // If the room timer was frozen, unfreeze/reset it now that the popup is closed if (roomCreatedTime == DateTime.MaxValue) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Unfreezing room timer (setting to DateTime.Now)."); roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; chatWarnedAboutNextRace = false; } isSubmittingSettings = false; } popupWasOpen = false; } if (string.IsNullOrEmpty(sceneName)) return; if (sceneName == "MainMenu") { HandleMainMenu(); } else if (sceneName == "MultiplayerMenu") { HandleMultiplayerMenu(); } else if (sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene") { popupWasOpen = false; HandleFlightLevel(); } }',
    'HandleGameRoom': 'private static void HandleGameRoom() { if (isLeaving) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Currently leaving room, ignoring GameRoom tick."); return; } // Flush any chat messages that were queued while the chat panel wasn\'t available yet // (e.g. sent from a Photon callback while still on the MultiplayerMenu screen). if (pendingRoomChatMessages.Count > 0) { string[] toSend = pendingRoomChatMessages.ToArray(); pendingRoomChatMessages.Clear(); foreach (string msg in toSend) { SendChatMessage(msg); } } if (roomCreatedTime == DateTime.MinValue || roomCreatedTime == DateTime.MaxValue) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Entered GameRoom. Starting room timer."); LogEvent("room_entered"); roomCreatedTime = DateTime.Now; lastActivityTime = DateTime.UtcNow; chatWarnedAboutNextRace = false; firstStartGameClickTime = DateTime.MinValue; // Apply a persisted max-players override (survives bot restarts), if one is configured. string maxPlayersPath = Path.Combine(pluginPath, "max_players.txt"); if (File.Exists(maxPlayersPath)) { int configuredMax; if (int.TryParse(File.ReadAllText(maxPlayersPath).Trim(), out configuredMax)) { int applied; string err; if (!SetRoomMaxPlayers(configuredMax, out applied, out err)) { UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to apply persisted max_players.txt ({configuredMax}): {err}"); } } } } // democracy-skip.md: prune skip votes from players who left the room, then // re-evaluate the majority (a departure can lower the required threshold enough // for the remaining votes to now win). No-ops when democracy mode is off. SkipCommand.CheckDisconnectedVoters(); double elapsed = (DateTime.Now - roomCreatedTime).TotalSeconds; ProcessClientScript(elapsed); // Auto-start: click the START button 15 seconds after entering the room to give players time to join if (GetAutoStart() && elapsed >= 15.0 && (DateTime.Now - lastStartGameClickedTime).TotalSeconds > 30.0) { string[] startNames = { "buttonStartGame", "btnStartGame", "StartGame", "btnStart", "buttonStart" }; Button startBtn = FindButtonByTextOrName("START GAME", startNames); if (startBtn == null) startBtn = FindButtonByTextOrName("START", startNames); if (startBtn != null && startBtn.gameObject.activeInHierarchy && startBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Auto-start: clicking START button."); lastStartGameClickedTime = DateTime.Now; if (firstStartGameClickTime == DateTime.MinValue) { firstStartGameClickTime = DateTime.Now; } startBtn.onClick.Invoke(); return; } } // Runtime watchdog: if Start Game has been clicked repeatedly but the scene never // transitioned into a flight level (race never actually loaded — the real-world // "Drawing Board" failure mode), treat this track as a runtime failure: blacklist it // for the rest of this session and recover to a known-good state. This is // defense-in-depth alongside the pre-launch validation, since a track can pass every // static check and still fail only when the race actually starts. if (firstStartGameClickTime != DateTime.MinValue) { double sinceFirstClick = (DateTime.Now - firstStartGameClickTime).TotalSeconds; if (sinceFirstClick > RaceLoadTimeoutSeconds) { string failKey = $"{targetEnvironment}|{targetTrackName}|{targetGameMode}"; UnityEngine.Debug.LogError($"[AutoLobbyPlugin] RUNTIME FAILURE: race did not load {sinceFirstClick:F0}s after Start Game click for \'{targetTrackName}\' (Env: {targetEnvironment}, Mode: {targetGameMode}). Blacklisting for this session."); sessionBlacklistedTracks.Add(failKey); // Structured JSON file event (A3): an autonomous plugin decision — a track that // failed to load at runtime is blacklisted for the rest of the session. LogJsonEvent("decision", ("kind", "track_blacklist"), ("detail", $"{targetEnvironment} - {targetTrackName} ({targetGameMode}) failed to load {sinceFirstClick:F0}s after Start Game")); firstStartGameClickTime = DateTime.MinValue; NavigateToMainMenu(); return; } } // Warn about the next track 10 seconds before rotation if (elapsed >= GetRotationInterval() - 15.0 && elapsed < GetRotationInterval()) { if (DateTime.Now.Second % 5 == 0) { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Close to rotation. chatWarned={chatWarnedAboutNextRace}, elapsed={elapsed:F1}s, target={GetRotationInterval() - 10.0:F1}s"); } } if (!chatWarnedAboutNextRace && elapsed >= GetRotationInterval() - 10.0 && elapsed < GetRotationInterval()) { chatWarnedAboutNextRace = true; string nextEnv, nextMode; int trackIdx; string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx); if (!string.IsNullOrEmpty(nextTrackName)) { SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} Up next: {FormatHighlight($"{nextEnv} - {nextTrackName}")}"); } else { UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] PeekNextTrackName returned null/empty."); } } if (skipRequested || elapsed >= GetRotationInterval()) { if (skipRequested) UnityEngine.Debug.Log("[AutoLobbyPlugin] Skip requested by admin — forcing rotation."); skipRequested = false; chatWarnedAboutNextRace = false; // Timer expired (or skip forced) inside waiting room, open change settings popup! string[] changeSettingsNames = { "buttonChangeRoomSettings", "btnChangeRoomSettings", "ChangeRoomSettings" }; Button changeSettingsBtn = FindButtonByTextOrName("CHANGE SETTINGS", changeSettingsNames); if (changeSettingsBtn != null && changeSettingsBtn.gameObject.activeInHierarchy && changeSettingsBtn.interactable) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Clicking CHANGE SETTINGS button."); changeSettingsBtn.onClick.Invoke(); roomCreatedTime = DateTime.MaxValue; // Freeze timer until settings updated } return; } HandleKeepAlive(); }',
    'CaptureLoadedTrack': '// Called at each settings-popup submit-success point (the same instant the track genuinely // becomes the loaded one). Snapshots the just-submitted target as the CURRENT track for // /info. (Track history is appended here too — see the /history slice.) private static void CaptureLoadedTrack() { currentTrackName = targetTrackName; currentEnvironment = targetEnvironment; trackHistory.Add($"{targetEnvironment} - {targetTrackName}"); while (trackHistory.Count > TrackHistoryMax) trackHistory.RemoveAt(0); // democracy-skip.md: a new track just loaded — stale skip votes from the // previous track must not carry over. skipVotes.Clear(); }',
    'PhotonContainerPrefix': 'private static bool PhotonContainerPrefix(object __instance, MethodBase __originalMethod, object[] __args) { try { string methodName = __originalMethod.Name; if (methodName == "OnLeftRoom" || methodName == "OnDisconnected") { UnityEngine.Debug.Log($"[AutoLobbyPlugin] Photon Callback: {methodName} detected. Immediately resetting lastInRoomTime to trigger lobby recovery."); lastInRoomTime = DateTime.MinValue; roomCreatedTime = DateTime.MinValue; isLeaving = false; } else if (methodName == "OnCreateRoomFailed" && __args != null && __args.Length >= 2) { // Not gated on pendingPrivateRoomRename: any create attempt (bot startup, // post-disconnect recreate, etc.) can hit a stale/occupied room name, not just // an explicit /private <name> request — always try to recover. HandleCreateRoomFailed((short)__args[0], __args[1] as string); } else if (methodName == "OnJoinRoomFailed" && joinByNamePanelSubmitted && __args != null && __args.Length >= 2) { HandleJoinByNameFailed((short)__args[0], __args[1] as string); } else if (methodName == "OnCreatedRoom") { roomOwnedByBot = true; // democracy-skip.md: a freshly created room starts with no skip votes. skipVotes.Clear(); if (pendingPrivateRoomRename) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Private room rename: new room created successfully."); QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room recreated as private. Join name: {FormatVariable($"{pendingPrivateRoomName}")}."); } pendingPrivateRoomRename = false; pendingPrivateRoomRenameStartTime = DateTime.MinValue; pendingJoinByName = false; joinByNamePanelSubmitted = false; } else if (methodName == "OnJoinedRoom" && joinByNamePanelSubmitted) { UnityEngine.Debug.Log("[AutoLobbyPlugin] Joined an existing room by name instead of creating one — bot does not own this room."); roomOwnedByBot = false; // democracy-skip.md: entering a (different) room starts with no skip votes. skipVotes.Clear(); QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} A room named \'{FormatVariable($"{pendingPrivateRoomName}")}\' already existed — joined it instead of creating a new one. <color={activeTheme.alertTagColor}><i>This bot is not the room owner and cannot control settings/rotation here.</i></color> Current host: please transfer host to this bot from the player list so it can control settings/rotation, or use /private with a different name to have the bot create its own room instead."); pendingPrivateRoomRename = false; pendingPrivateRoomRenameStartTime = DateTime.MinValue; pendingJoinByName = false; joinByNamePanelSubmitted = false; } else if (methodName == "OnMasterClientSwitched" && __args != null && __args.Length >= 1) { HandleMasterClientSwitched(__args[0]); } else if (methodName == "OnPlayerEnteredRoom" && __args != null && __args.Length >= 1) { LogPlayerPresenceEvent("player_join", __args[0]); } else if (methodName == "OnPlayerLeftRoom" && __args != null && __args.Length >= 1) { LogPlayerPresenceEvent("player_leave", __args[0]); } System.Collections.IList list = __instance as System.Collections.IList; if (list == null) return true; // Copy targets to avoid collection modified exceptions object[] targets; lock (list) { targets = new object[list.Count]; list.CopyTo(targets, 0); } // Find the interface type that defines this callback Type interfaceType = null; foreach (var iface in __instance.GetType().GetInterfaces()) { if (iface.Name.EndsWith("Callbacks") || iface.Name.Contains("Callback")) { interfaceType = iface; break; } } if (interfaceType == null) return true; // Resolve the interface method matching name and parameter types var paramTypes = __originalMethod.GetParameters().Select(p => p.ParameterType).ToArray(); MethodInfo interfaceMethod = interfaceType.GetMethod(__originalMethod.Name, paramTypes); if (interfaceMethod == null) return true; foreach (var callback in targets) { if (callback == null) continue; try { interfaceMethod.Invoke(callback, __args); } catch (Exception ex) { // Log the actual underlying exception Exception realEx = ex.InnerException ?? ex; UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Suppressed exception in {interfaceType.Name} listener ({callback.GetType().FullName}): {realEx}"); } } return false; // Skip the original looping method which would abort on exception } catch (Exception ex) { UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PhotonContainerPrefix: {ex}"); return true; // Fallback to original method on error } }',
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
