using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Reflection;
using System.Collections.Generic;
using System.Linq;
using BepInEx;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;
using Liftoff.Multiplayer;
using Liftoff.Multiplayer.GameSetup;
using Photon.Realtime;
using ExitGames.Client.Photon;
using HarmonyLib;

namespace LiftoffAutoLobby
{
    // MODE: server-only — excluded/disabled in client mode, see
    // docs/features/backlog/dual-mode-plugin-server-and-client.md
    // Room creation and recovery: ConfigureAndCreateRoom (the settings-popup submit
    //     flow), popup element accessors, the track x game-mode availability dump
    //     machinery, private-room rename, and the create/join-by-name failure and
    //     fallback flows.
    //     † /private <name> currently reuses this create/join flow, so client mode may
    //     need a slice of this file eventually -- see the dual-mode doc's open question.
    public partial class AutoLobbyPlugin
    {

        // Nested Environment x GameMode track-availability dump. Regenerated once per process
        // launch (not gated on file-existence like the old dump) so it can't go stale relative
        // to Workshop subscription changes between runs. Scoped to a fixed candidate mode list
        // (mirrors the /mode admin command's supported values) to bound the combinatorial cost.
        private static readonly string[] TrackModeDumpCandidateModes = { "Infinite Race", "Classic Race", "Dropout Race", "Survival" };
        private static bool trackModeDumpDoneThisSession = false;
        private static bool isDumpingTrackModes = false;
        private static int dumpEnvIndex2 = 0;
        private static int dumpModeIndex2 = 0;
        private static Dictionary<string, Dictionary<string, List<string>>> dumpedTrackModeMap = new Dictionary<string, Dictionary<string, List<string>>>();

        private static void RecordTrackModeDump(string envName, string modeName, List<string> tracks)
        {
            if (!dumpedTrackModeMap.TryGetValue(envName, out var modeMap))
            {
                modeMap = new Dictionary<string, List<string>>();
                dumpedTrackModeMap[envName] = modeMap;
            }
            modeMap[modeName] = tracks;
        }

        private static string JsonEscape(string s) => s.Replace("\"", "\\\"");

        // Union of tracks per environment across all dumped modes — keeps the existing flat
        // schema so gather_tracks.py's ui_tracks_dump.json consumer contract is unchanged.
        private static void WriteLegacyUiTracksDump(Dictionary<string, Dictionary<string, List<string>>> data)
        {
            string dumpFilePath = Path.Combine(pluginPath, "ui_tracks_dump.json");
            try
            {
                List<string> lines = new List<string>();
                lines.Add("{");
                int envCount = 0;
                foreach (var envEntry in data)
                {
                    envCount++;
                    var unionTracks = new List<string>();
                    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    foreach (var modeEntry in envEntry.Value)
                    {
                        foreach (var track in modeEntry.Value)
                        {
                            if (seen.Add(track)) unionTracks.Add(track);
                        }
                    }
                    List<string> trackListStr = new List<string>();
                    foreach (var track in unionTracks) trackListStr.Add($"\"{JsonEscape(track)}\"");
                    string comma = (envCount < data.Count) ? "," : "";
                    lines.Add($"  \"{JsonEscape(envEntry.Key)}\": [{string.Join(", ", trackListStr.ToArray())}]{comma}");
                }
                lines.Add("}");
                File.WriteAllLines(dumpFilePath, lines.ToArray());
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Legacy UI tracks dump saved to: {dumpFilePath}");
            }
            catch (Exception dumpEx)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write legacy UI tracks dump: {dumpEx}");
            }
        }

        // New ground-truth file: {Environment: {GameMode: [trackNames]}}. This is what the
        // Python orchestrator cross-validates resolved playlist entries against before writing
        // tracks_to_rotate.txt (see resolve_and_write_playlist() in run_headless_lobby.py).
        private static void WriteTrackModeAvailabilityDump(Dictionary<string, Dictionary<string, List<string>>> data)
        {
            string dumpFilePath = Path.Combine(pluginPath, "track_mode_availability.json");
            try
            {
                List<string> lines = new List<string>();
                lines.Add("{");
                int envCount = 0;
                foreach (var envEntry in data)
                {
                    envCount++;
                    lines.Add($"  \"{JsonEscape(envEntry.Key)}\": {{");
                    int modeCount = 0;
                    foreach (var modeEntry in envEntry.Value)
                    {
                        modeCount++;
                        List<string> trackListStr = new List<string>();
                        foreach (var track in modeEntry.Value) trackListStr.Add($"\"{JsonEscape(track)}\"");
                        string modeComma = (modeCount < envEntry.Value.Count) ? "," : "";
                        lines.Add($"    \"{JsonEscape(modeEntry.Key)}\": [{string.Join(", ", trackListStr.ToArray())}]{modeComma}");
                    }
                    string envComma = (envCount < data.Count) ? "," : "";
                    lines.Add($"  }}{envComma}");
                }
                lines.Add("}");
                File.WriteAllLines(dumpFilePath, lines.ToArray());
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Track/mode availability dump saved to: {dumpFilePath}");
            }
            catch (Exception dumpEx)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write track/mode availability dump: {dumpEx}");
            }
        }

        // Top-level environment keys are always written at exactly 2-space indent by
        // WriteTrackModeAvailabilityDump (mode keys nest one level deeper, at 4 spaces), so a
        // plain indent-width scan is enough to recover them without a JSON parser dependency.
        private static HashSet<string> ReadCachedTrackModeDumpEnvNames(string path)
        {
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            try
            {
                foreach (string rawLine in File.ReadAllLines(path))
                {
                    if (rawLine.Length < 3 || rawLine[0] != ' ' || rawLine[1] != ' ' || rawLine[2] == ' ') continue;
                    string line = rawLine.TrimStart();
                    if (!line.StartsWith("\"")) continue;
                    int endQuote = line.IndexOf('"', 1);
                    if (endQuote <= 1) continue;
                    names.Add(line.Substring(1, endQuote - 1));
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to read cached track_mode_availability.json: {ex.Message}");
            }
            return names;
        }

        // Skips the (multi-minute) dropdown-driven dump when a previous session's dump already
        // covers the same set of environments — the common case during dev iteration, where a
        // plugin rebuild forces a full bot restart but nothing about the Workshop
        // subscriptions/installed environments actually changed. Fails closed (returns false, so
        // the full dump runs) on anything unexpected, since a stale/incomplete cached file is a
        // worse outcome than paying the one-time dump cost.
        private static bool TryReuseCachedTrackModeDump(ContentSettingsPanel contentSettings)
        {
            string dumpFilePath = Path.Combine(pluginPath, "track_mode_availability.json");
            if (!File.Exists(dumpFilePath)) return false;

            LiftoffDropdown dropdownEnvironment = GetContentDropdownEnvironment(contentSettings);
            if (dropdownEnvironment == null || dropdownEnvironment.options.Count == 0) return false;

            var liveEnvNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var opt in dropdownEnvironment.options) liveEnvNames.Add(opt.text);

            var cachedEnvNames = ReadCachedTrackModeDumpEnvNames(dumpFilePath);
            if (cachedEnvNames.Count == 0 || !liveEnvNames.SetEquals(cachedEnvNames))
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Cached track/mode dump is stale or unreadable (live {liveEnvNames.Count} envs vs cached {cachedEnvNames.Count}) — re-dumping.");
                return false;
            }

            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Reusing cached track/mode availability dump from a previous session ({cachedEnvNames.Count} environments match) — skipping re-dump.");
            return true;
        }

        private static bool TrySelectCustomContentTab(PopupQuickPlayMultiplayerSetup popup)
        {
            if (popup == null) return false;
            Button[] allButtons = popup.GetComponentsInChildren<Button>(true);
            foreach (Button btn in allButtons)
            {
                if (btn == null || !btn.gameObject.activeInHierarchy || !btn.interactable) continue;
                string txt = GetButtonText(btn);
                string name = btn.name ?? "";
                bool isCustomTab =
                    txt.IndexOf("custom", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    txt.IndexOf("workshop", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    txt.IndexOf("my tracks", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("custom", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("workshop", StringComparison.OrdinalIgnoreCase) >= 0;
                if (isCustomTab)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking custom content tab: name='{name}' text='{txt}'");
                    btn.onClick.Invoke();
                    return true;
                }
            }
            UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] No custom content tab found in popup.");
            return false;
        }

        private static void ConfigureAndCreateRoom(PopupQuickPlayMultiplayerSetup popup)
        {
            ContentSettingsPanel contentSettings = GetPopupContentSettings(popup);
            if (contentSettings != null)
            {
                if (!isDumpingTrackModes && !trackModeDumpDoneThisSession)
                {
                    if (TryReuseCachedTrackModeDump(contentSettings))
                    {
                        trackModeDumpDoneThisSession = true;
                    }
                    else
                    {
                        isDumpingTrackModes = true;
                        dumpEnvIndex2 = 0;
                        dumpModeIndex2 = 0;
                        dumpedTrackModeMap.Clear();
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Starting Environment x GameMode track availability dump ({TrackModeDumpCandidateModes.Length} candidate modes)...");
                    }
                }

                if (isDumpingTrackModes)
                {
                    LiftoffDropdown dropdownEnvironment = GetContentDropdownEnvironment(contentSettings);
                    LiftoffDropdown dropdownGameMode = GetContentDropdownGameMode(contentSettings);
                    LiftoffDropdown dropdownContent = GetContentDropdownContent(contentSettings);
                    if (dropdownEnvironment != null && dropdownGameMode != null && dropdownContent != null)
                    {
                        if (dumpEnvIndex2 < dropdownEnvironment.options.Count)
                        {
                            if (dropdownEnvironment.value != dumpEnvIndex2)
                            {
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Dump: Selecting environment index {dumpEnvIndex2} ({dropdownEnvironment.options[dumpEnvIndex2].text})");
                                dropdownEnvironment.value = dumpEnvIndex2;
                                dropdownEnvironment.onValueChanged.Invoke(dumpEnvIndex2);
                                return; // Let the UI update next frame
                            }

                            string envName = dropdownEnvironment.options[dumpEnvIndex2].text;

                            if (dumpModeIndex2 < TrackModeDumpCandidateModes.Length)
                            {
                                string modeName = TrackModeDumpCandidateModes[dumpModeIndex2];
                                int modeIdx = -1;
                                for (int i = 0; i < dropdownGameMode.options.Count; i++)
                                {
                                    if (dropdownGameMode.options[i].text.Equals(modeName, StringComparison.OrdinalIgnoreCase))
                                    {
                                        modeIdx = i;
                                        break;
                                    }
                                }

                                if (modeIdx == -1)
                                {
                                    // This environment doesn't offer this mode at all
                                    RecordTrackModeDump(envName, modeName, new List<string>());
                                    dumpModeIndex2++;
                                    return;
                                }

                                if (dropdownGameMode.value != modeIdx)
                                {
                                    dropdownGameMode.value = modeIdx;
                                    dropdownGameMode.onValueChanged.Invoke(modeIdx);
                                    return; // Let the UI update next frame
                                }

                                var tracks = new List<string>();
                                for (int i = 0; i < dropdownContent.options.Count; i++)
                                {
                                    tracks.Add(dropdownContent.options[i].text);
                                }
                                RecordTrackModeDump(envName, modeName, tracks);
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Dump: env='{envName}' mode='{modeName}' -> {tracks.Count} tracks");

                                dumpModeIndex2++;
                                return; // Wait for next tick
                            }
                            else
                            {
                                dumpModeIndex2 = 0;
                                dumpEnvIndex2++;
                                return;
                            }
                        }
                        else
                        {
                            WriteLegacyUiTracksDump(dumpedTrackModeMap);
                            WriteTrackModeAvailabilityDump(dumpedTrackModeMap);
                            isDumpingTrackModes = false;
                            trackModeDumpDoneThisSession = true;
                            UnityEngine.Debug.Log("[AutoLobbyPlugin] Track/mode availability dump complete.");
                        }
                    }
                    return; // Pause room configuration during dumping
                }
            }

            if (string.IsNullOrEmpty(targetTrackName))
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] targetTrackName is empty, canceling popup.");
                Button cancelBtn = GetPopupCancelButton(popup);
                if (cancelBtn != null) cancelBtn.onClick.Invoke();
                return;
            }

            // Dump button listeners for debugging
            if (DateTime.Now.Second % 5 == 0)
            {
                DumpButtonListeners("buttonCreateGame", GetPopupCreateButton(popup));
                DumpButtonListeners("buttonUpdateGame", GetPopupUpdateButton(popup));
                var fieldActive = typeof(PopupQuickPlayMultiplayerSetup).GetField("activeButton", BindingFlags.NonPublic | BindingFlags.Instance);
                if (fieldActive != null)
                {
                    Button activeBtnDebug = (Button)fieldActive.GetValue(popup);
                    DumpButtonListeners("activeButton", activeBtnDebug);
                }
            }

            // 1. Configure Room settings
            RoomSettingsPanel roomSettings = GetPopupRoomSettings(popup);
            if (roomSettings != null)
            {
                bool makePrivate = true;
                string privacyPath = Path.Combine(pluginPath, "room_private.txt");
                if (File.Exists(privacyPath))
                {
                    string content = File.ReadAllText(privacyPath).Trim();
                    if (content.Equals("false", StringComparison.OrdinalIgnoreCase))
                    {
                        makePrivate = false;
                    }
                }

                // Set toggle first so the name panel activates before we write to the InputField.
                // The room name InputField lives inside panelPrivateRoom which starts inactive;
                // Unity InputField.onValueChanged does not fire reliably on inactive objects.
                Toggle togglePrivate = GetRoomSettingsTogglePrivate(roomSettings);
                if (togglePrivate != null && togglePrivate.isOn != makePrivate)
                {
                    togglePrivate.isOn = makePrivate;
                }

                if (makePrivate)
                {
                    InputField inputRoomName = GetRoomSettingsInputField(roomSettings);
                    if (inputRoomName != null && inputRoomName.text != targetLobbyName)
                    {
                        inputRoomName.text = targetLobbyName;
                    }
                }
            }

            // 2. Configure Content settings (Environment, GameMode, Track)
            // Environment is set FIRST: selecting it can cause the game to re-filter/rebuild
            // the GameMode and Content dropdown options (cascading filters), so GameMode must
            // be (re)verified AFTER Environment, not before, or a valid choice can be silently
            // invalidated and never reapplied.
            contentSettings = GetPopupContentSettings(popup);
            if (contentSettings != null)
            {
                // Set Environment
                LiftoffDropdown dropdownEnvironment = GetContentDropdownEnvironment(contentSettings);
                if (dropdownEnvironment != null)
                {
                    int envIndex = -1;
                    for (int i = 0; i < dropdownEnvironment.options.Count; i++)
                    {
                        if (dropdownEnvironment.options[i].text.Equals(targetEnvironment, StringComparison.OrdinalIgnoreCase))
                        {
                            envIndex = i;
                            break;
                        }
                    }
                    if (envIndex != -1)
                    {
                        if (dropdownEnvironment.value != envIndex)
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Changing Environment dropdown to: {targetEnvironment}");
                            dropdownEnvironment.value = envIndex;
                            dropdownEnvironment.onValueChanged.Invoke(envIndex);
                            return; // Let the UI update next frame
                        }
                    }
                    else
                    {
                        if (DateTime.Now.Second % 5 == 0)
                        {
                            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Environment '{targetEnvironment}' not found in dropdown. Available Environments:");
                            for (int i = 0; i < dropdownEnvironment.options.Count; i++)
                            {
                                UnityEngine.Debug.Log($"  - Env {i}: '{dropdownEnvironment.options[i].text}'");
                            }
                        }
                    }
                }

                // Log all dropdown options for debugging
                if (DateTime.Now.Second % 15 == 0)
                {
                    LiftoffDropdown dropdownEnvironmentDebug = GetContentDropdownEnvironment(contentSettings);
                    if (dropdownEnvironmentDebug != null)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Environment dropdown options (Count: {dropdownEnvironmentDebug.options.Count}, Current Value: {dropdownEnvironmentDebug.value}):");
                        for (int i = 0; i < dropdownEnvironmentDebug.options.Count; i++)
                        {
                            UnityEngine.Debug.Log($"  - Env Option {i}: '{dropdownEnvironmentDebug.options[i].text}'");
                        }
                    }
                    LiftoffDropdown dropdownContentDebug = GetContentDropdownContent(contentSettings);
                    if (dropdownContentDebug != null)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Content dropdown options (Count: {dropdownContentDebug.options.Count}, Current Value: {dropdownContentDebug.value}):");
                        for (int i = 0; i < dropdownContentDebug.options.Count; i++)
                        {
                            UnityEngine.Debug.Log($"  - Content Option {i}: '{dropdownContentDebug.options[i].text}'");
                        }
                    }
                }

                // Set GameMode (after Environment, since Environment selection can re-filter these options)
                LiftoffDropdown dropdownGameMode = GetContentDropdownGameMode(contentSettings);
                if (dropdownGameMode != null)
                {
                    int modeIndex = -1;
                    for (int i = 0; i < dropdownGameMode.options.Count; i++)
                    {
                        if (dropdownGameMode.options[i].text.Equals(targetGameMode, StringComparison.OrdinalIgnoreCase))
                        {
                            modeIndex = i;
                            break;
                        }
                    }
                    if (modeIndex != -1)
                    {
                        if (dropdownGameMode.value != modeIndex)
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Changing GameMode dropdown to: {targetGameMode}");
                            dropdownGameMode.value = modeIndex;
                            dropdownGameMode.onValueChanged.Invoke(modeIndex);
                            return; // Let the UI update next frame
                        }
                    }
                    else
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] GameMode '{targetGameMode}' not found in dropdown. Available GameModes:");
                        for (int i = 0; i < dropdownGameMode.options.Count; i++)
                        {
                            UnityEngine.Debug.Log($"  - GameMode {i}: '{dropdownGameMode.options[i].text}'");
                        }
                    }
                }

                // Set Track
                LiftoffDropdown dropdownContent = GetContentDropdownContent(contentSettings);
                if (dropdownContent != null)
                {
                    int trackIndex = -1;
                    for (int i = 0; i < dropdownContent.options.Count; i++)
                    {
                        if (dropdownContent.options[i].text.IndexOf(targetTrackName, StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            trackIndex = i;
                            break;
                        }
                    }

                    if (trackIndex != -1)
                    {
                        if (dropdownContent.value != trackIndex)
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Changing Track dropdown to: {targetTrackName} (index: {trackIndex})");
                            dropdownContent.value = trackIndex;
                            dropdownContent.onValueChanged.Invoke(trackIndex);
                            return; // Let the UI update next frame
                        }
                    }
                    else
                    {
                        double popupAge = (DateTime.Now - popupOpenedTime).TotalSeconds;

                        // Once, try switching to the Custom content tab
                        if (!triedCustomContentTab && popupAge > 3.0)
                        {
                            triedCustomContentTab = true;
                            TrySelectCustomContentTab(popup);
                            return; // Let dropdown refresh next tick
                        }

                        // Timeout: cancel popup and advance rotation
                        if (popupAge > 45.0)
                        {
                            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Track '{targetTrackName}' not found after {popupAge:F0}s — skipping.");
                            Button cancelBtn = GetPopupCancelButton(popup);
                            if (cancelBtn != null && cancelBtn.gameObject.activeInHierarchy)
                                cancelBtn.onClick.Invoke();
                            targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode);
                            triedCustomContentTab = false;
                            isSubmittingSettings = false;
                            return;
                        }

                        if (DateTime.Now.Second % 5 == 0)
                        {
                            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Track '{targetTrackName}' not found in dropdown ({popupAge:F0}s). Available options:");
                            for (int i = 0; i < dropdownContent.options.Count; i++)
                                UnityEngine.Debug.Log($"  - Option {i}: '{dropdownContent.options[i].text}'");
                        }
                        return;
                    }
                }
            }

            // 3. Click the active button or fallback to Create/Update based on room state
            var fieldActiveButton = typeof(PopupQuickPlayMultiplayerSetup).GetField("activeButton", BindingFlags.NonPublic | BindingFlags.Instance);
            Button activeBtn = fieldActiveButton != null ? (Button)fieldActiveButton.GetValue(popup) : null;

            bool settingsValid = roomSettings != null && roomSettings.GameSettingsValid() &&
                                  contentSettings != null && contentSettings.GameSettingsValid();

            if (activeBtn != null && activeBtn.gameObject.activeInHierarchy)
            {
                if (activeBtn.interactable || settingsValid)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking activeButton: {activeBtn.name} (interactable={activeBtn.interactable}, settingsValid={settingsValid})");

                    popupSubmittedTime = DateTime.Now;
                    isSubmittingSettings = true;
                    activeBtn.onClick.Invoke();
                    CaptureLoadedTrack();

                    // Reset room timer if we are in a room
                    GameObject gameRoomObj = GameObject.Find("GameRoom");
                    bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy);
                    if (inRoom)
                    {
                        roomCreatedTime = DateTime.Now;
                        lastActivityTime = DateTime.UtcNow;
                        chatWarnedAboutNextRace = false;
                    }
                    return;
                }
            }

            if (settingsValid)
            {
                // Check if GameRoom is active
                GameObject gameRoomObj = GameObject.Find("GameRoom");
                bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy);

                if (inRoom)
                {
                    // We are in a room, we want to update settings
                    Button updateBtn = GetPopupUpdateButton(popup);
                    if (updateBtn != null && updateBtn.gameObject.activeInHierarchy && updateBtn.interactable)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Fallback: Clicking Update Game settings button inside lobby.");
    
                        popupSubmittedTime = DateTime.Now;
                        isSubmittingSettings = true;
                        updateBtn.onClick.Invoke();
                        CaptureLoadedTrack();
                        roomCreatedTime = DateTime.Now; // Reset the rotation timer!
                        lastActivityTime = DateTime.UtcNow;
                        chatWarnedAboutNextRace = false;
                    }
                }
                else
                {
                    // We are not in a room, we want to create a new room
                    Button createBtn = GetPopupCreateButton(popup);
                    if (createBtn != null && createBtn.gameObject.activeInHierarchy && createBtn.interactable)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Fallback: Clicking Create Game button to host new room.");
    
                        popupSubmittedTime = DateTime.Now;
                        isSubmittingSettings = true;
                        createBtn.onClick.Invoke();
                        CaptureLoadedTrack();
                    }
                }
            }
        }

        // Reflection Helpers to access private fields on PopupQuickPlayMultiplayerSetup
        private static RoomSettingsPanel GetPopupRoomSettings(PopupQuickPlayMultiplayerSetup popup)
        {
            var field = typeof(PopupQuickPlayMultiplayerSetup).GetField("panelRoomSettings", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (RoomSettingsPanel)field.GetValue(popup) : null;
        }

        private static ContentSettingsPanel GetPopupContentSettings(PopupQuickPlayMultiplayerSetup popup)
        {
            var field = typeof(PopupQuickPlayMultiplayerSetup).GetField("panelContentSetupPanel", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (ContentSettingsPanel)field.GetValue(popup) : null;
        }

        private static Button GetPopupCreateButton(PopupQuickPlayMultiplayerSetup popup)
        {
            var field = typeof(PopupQuickPlayMultiplayerSetup).GetField("buttonCreateGame", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (Button)field.GetValue(popup) : null;
        }

        private static Button GetPopupUpdateButton(PopupQuickPlayMultiplayerSetup popup)
        {
            var field = typeof(PopupQuickPlayMultiplayerSetup).GetField("buttonUpdateGame", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (Button)field.GetValue(popup) : null;
        }

        private static Button GetPopupCancelButton(PopupQuickPlayMultiplayerSetup popup)
        {
            var field = typeof(PopupQuickPlayMultiplayerSetup).GetField("buttonCancel", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (Button)field.GetValue(popup) : null;
        }

        // Reflection Helpers to access private fields on RoomSettingsPanel
        private static InputField GetRoomSettingsInputField(RoomSettingsPanel panel)
        {
            var field = typeof(RoomSettingsPanel).GetField("inputFieldRoomName", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (InputField)field.GetValue(panel) : null;
        }

        private static Toggle GetRoomSettingsTogglePrivate(RoomSettingsPanel panel)
        {
            var field = typeof(RoomSettingsPanel).GetField("togglePrivateRoom", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (Toggle)field.GetValue(panel) : null;
        }

        // Reflection Helpers to access private fields on ContentSettingsPanel
        private static LiftoffDropdown GetContentDropdownGameMode(ContentSettingsPanel panel)
        {
            var field = typeof(ContentSettingsPanel).GetField("dropdownGameModeSelection", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (LiftoffDropdown)field.GetValue(panel) : null;
        }

        private static LiftoffDropdown GetContentDropdownEnvironment(ContentSettingsPanel panel)
        {
            var field = typeof(ContentSettingsPanel).GetField("dropdownEnvironmentSelection", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (LiftoffDropdown)field.GetValue(panel) : null;
        }

        private static LiftoffDropdown GetContentDropdownContent(ContentSettingsPanel panel)
        {
            var field = typeof(ContentSettingsPanel).GetField("dropdownContentSelection", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (LiftoffDropdown)field.GetValue(panel) : null;
        }

        private static List<ShareableContent> GetContentSelectedContent(ContentSettingsPanel panel)
        {
            var field = typeof(ContentSettingsPanel).GetField("selectedContent", BindingFlags.NonPublic | BindingFlags.Instance);
            return field != null ? (List<ShareableContent>)field.GetValue(panel) : null;
        }

        // Kicks off the leave -> recreate-with-new-name flow for `/private <name>`.
        // The actual room creation is handled by the existing ConfigureAndCreateRoom path once the
        // bot is back at the MultiplayerMenu lobby list (lobby_name.txt / room_private.txt already updated).
        private static void BeginPrivateRoomRename(string newName, string adminName)
        {
            pendingPrivateRoomRename = true;
            pendingPrivateRoomRenameStartTime = DateTime.Now;
            pendingPrivateRoomName = newName;
            pendingPrivateRoomAdmin = adminName;
            pendingJoinByName = false;
            joinByNamePanelSubmitted = false;
            joinByNameButtonClickedTime = DateTime.MinValue;

            try
            {
                File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "true");
                File.WriteAllText(Path.Combine(pluginPath, "lobby_name.txt"), newName);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write room_private.txt/lobby_name.txt: {ex.Message}");
            }

            targetLobbyName = newName; // picked up immediately if the popup is already open on next tick

            bool wasInRoom = GetPhotonBoolProperty("InRoom");
            SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Recreating room as private with name {FormatVariable($"{newName}")}. Current players will be disconnected.");
            if (wasInRoom)
            {
                if (!TryLeaveCurrentRoom())
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Could not call LeaveRoom() — will rely on scene/menu recovery instead.");
                }
            }
        }

        // Handles ANY failed room creation, not just ones triggered by /private <name> — a stale
        // lobby_name.txt or a leftover room from a previous session can collide just as easily
        // during a normal bot-startup create. Always attempts the join-by-name -> public-fallback
        // recovery chain rather than leaving the bot stuck on the settings popup forever.
        private static void HandleCreateRoomFailed(short errorCode, string message)
        {
            isSubmittingSettings = false; // unstick RunTick's "waiting for popup to close" loop
            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Create room failed ({errorCode}) {message}");
            LogJsonEvent("error",
                ("message", $"Create room failed ({errorCode}): {message}"),
                ("context", "create_room_failed"));
            if (errorCode == ErrorCode.GameIdAlreadyExists)
            {
                pendingPrivateRoomName = targetLobbyName;
                pendingJoinByName = true;
                pendingJoinByNameSetTime = DateTime.Now;
                joinByNamePanelSubmitted = false;
                joinByNameButtonClickedTime = DateTime.MinValue;
                QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} A room named '{FormatVariable($"{pendingPrivateRoomName}")}' already exists — attempting to join it instead.");
            }
            else if (pendingPrivateRoomRename)
            {
                QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Failed to create private room '{FormatVariable($"{pendingPrivateRoomName}")}': {message}");
                pendingPrivateRoomRename = false;
                pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                pendingJoinByName = false;
                joinByNamePanelSubmitted = false;
            }
            else
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Room creation failed for a reason other than a name collision — will retry with the same settings next tick.");
            }
        }

        private static void HandleJoinByNameFailed(short errorCode, string message)
        {
            isSubmittingSettings = false;
            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Join by name failed ({errorCode}) {message}");
            LogJsonEvent("error",
                ("message", $"Join by name failed ({errorCode}): {message}"),
                ("context", "join_by_name_failed"));
            if (errorCode == ErrorCode.GameFull || errorCode == ErrorCode.GameClosed || errorCode == ErrorCode.GameDoesNotExist)
            {
                FallBackToPublicRoom($"Room '{pendingPrivateRoomName}' couldn't be joined ({message}).");
            }
            else
            {
                QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Failed to join room '{FormatVariable($"{pendingPrivateRoomName}")}': {message}. Giving up.");
                pendingPrivateRoomRename = false;
                pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                pendingJoinByName = false;
                joinByNamePanelSubmitted = false;
            }
        }

        // Last-resort recovery: abandon the private-name attempt and let the bot create a public
        // room instead. Liftoff auto-generates a random public room name (see
        // RoomSettingsPanel.ApplyToGameSettings/GenerateRoomName), so this can't collide again.
        private static void FallBackToPublicRoom(string reasonForChat)
        {
            QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} {reasonForChat} Falling back to a public room.");
            try
            {
                File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "false");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write room_private.txt: {ex.Message}");
            }
            pendingPrivateRoomRename = false;
            pendingPrivateRoomRenameStartTime = DateTime.MinValue;
            pendingJoinByName = false;
            joinByNamePanelSubmitted = false;
        }

        // Drives the "join by name" sub-flow once OnCreateRoomFailed(GameIdAlreadyExists) has set pendingJoinByName.
        // Called every tick from HandleMultiplayerMenu while on the lobby list screen.
        private static void ProcessJoinByNameFlow()
        {
            if (!pendingJoinByName || joinByNamePanelSubmitted) return;

            // Hard timeout on the whole flow, independent of which specific step is stuck —
            // never leave the bot spinning here indefinitely.
            if (pendingJoinByNameSetTime != DateTime.MinValue &&
                (DateTime.Now - pendingJoinByNameSetTime).TotalSeconds > 15.0)
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Join-by-name flow timed out.");
                FallBackToPublicRoom($"Could not join room '{pendingPrivateRoomName}' in time.");
                return;
            }

            if (joinByNameButtonClickedTime == DateTime.MinValue)
            {
                // Confirmed via live UI dump: GameObject name is "buttonJoinByName", button text
                // is "Join game by name" (not the field name "buttonJoinRoomByName" guessed from
                // the decompiled class, and not an exact "JOIN BY NAME" text match).
                string[] joinByNameNames = { "buttonJoinByName", "btnJoinByName", "JoinByName" };
                Button joinByNameBtn = FindButtonByTextOrName("Join game by name", joinByNameNames);
                if (joinByNameBtn == null)
                {
                    foreach (Button b in Resources.FindObjectsOfTypeAll<Button>())
                    {
                        if (b == null || !b.gameObject.activeInHierarchy) continue;
                        string txt = GetButtonText(b);
                        if (!string.IsNullOrEmpty(txt) && txt.IndexOf("join", StringComparison.OrdinalIgnoreCase) >= 0 &&
                            txt.IndexOf("name", StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            joinByNameBtn = b;
                            break;
                        }
                    }
                }
                if (joinByNameBtn != null && joinByNameBtn.gameObject.activeInHierarchy && joinByNameBtn.interactable)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Clicking Join By Name button to reveal the join panel.");
                    joinByNameBtn.onClick.Invoke();
                    joinByNameButtonClickedTime = DateTime.Now;
                }
                return;
            }

            // Give the panel a moment to activate before looking for its contents.
            if ((DateTime.Now - joinByNameButtonClickedTime).TotalSeconds < 1.5) return;

            // Confirmed via live UI dump: GameObject name is "InputFieldName", placeholder text
            // is "Enter game name" (not "fieldRoomName" guessed from the decompiled class).
            string[] joinButtonNames = { "buttonJoin", "btnJoin", "Join" };
            InputField roomNameField = FindInputFieldByName(JoinByNameRoomFieldNames, "game name");
            // Scope the Join-button search to the input field's own panel — a global search would
            // ambiguously match one of the many per-row "buttonJoin" buttons in the room list.
            Button joinBtn = roomNameField != null
                ? (FindButtonInHierarchy(roomNameField.transform.parent, joinButtonNames)
                   ?? FindButtonInHierarchy(roomNameField.transform.parent?.parent, joinButtonNames))
                : null;

            if (roomNameField == null || joinBtn == null)
            {
                // Panel may still be initializing; give up after 10s so we don't spin forever.
                if ((DateTime.Now - joinByNameButtonClickedTime).TotalSeconds > 10.0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Could not find join-by-name panel fields.");
                    FallBackToPublicRoom($"Could not locate the join-by-name UI for '{pendingPrivateRoomName}'.");
                }
                return;
            }

            roomNameField.text = pendingPrivateRoomName;
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Submitting join-by-name for room '{pendingPrivateRoomName}'.");
            joinBtn.onClick.Invoke();
            joinByNamePanelSubmitted = true;
        }


        private static MethodInfo isConnectedMethod = null;

        private static void ResolveMultiplayerClientCheck()
        {
            try
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Resolving multiplayer client readiness check...");
                Assembly asm = Assembly.Load("Assembly-CSharp");
                Type panelType = asm.GetType("Liftoff.Multiplayer.GameSetup.RoomSettingsPanel");
                if (panelType != null)
                {
                    MethodInfo fillMethod = panelType.GetMethod("FillMaxNrOfPlayersDropdown", BindingFlags.NonPublic | BindingFlags.Instance);
                    if (fillMethod != null)
                    {
                        byte[] il = fillMethod.GetMethodBody().GetILAsByteArray();
                        if (il[0] == 0x28) // call
                        {
                            int token = BitConverter.ToInt32(il, 1);
                            isConnectedMethod = (MethodInfo)fillMethod.Module.ResolveMember(token);
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Dynamically resolved IsConnected check: {isConnectedMethod.DeclaringType.FullName}::{isConnectedMethod.Name}");
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to dynamically resolve IsConnected check: {ex}");
            }
        }

        private static bool IsMultiplayerClientReady()
        {
            bool ready = false;
            if (isConnectedMethod != null)
            {
                try
                {
                    ready = (bool)isConnectedMethod.Invoke(null, null);
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error calling resolved IsConnected: {ex.Message}");
                }
            }
            bool photonReady = GetPhotonIsConnectedAndReady();
            if (DateTime.Now.Second % 5 == 0)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] IsMultiplayerClientReady: isConnectedMethod={ready}, photonReady={photonReady}");
            }
            return ready || photonReady;
        }
    }
}
