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
    // MODE: shared
    // In-room tick behavior: HandleGameRoom/HandleFlightLevel, keep-alive, pro tips,
    //     maintenance cancellation, and the scripted client-script playback used by the
    //     black-box test harness.
    public partial class AutoLobbyPlugin
    {


        // Sends the next due scripted line, if any, once elapsed room time reaches its
        // scheduled delay. Called every HandleGameRoom tick; no-op when no script is loaded.
        private static void ProcessClientScript(double elapsedSinceRoomEntered)
        {
            if (clientScriptNextIndex >= clientScriptSteps.Count) return;
            var step = clientScriptSteps[clientScriptNextIndex];
            if (elapsedSinceRoomEntered >= step.Item1)
            {
                SendChatMessage(step.Item2);
                LogEvent("client_script_step", ("index", clientScriptNextIndex.ToString()), ("message", step.Item2));
                clientScriptNextIndex++;
            }
        }

        private static void HandleGameRoom()
        {
            if (isLeaving)
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Currently leaving room, ignoring GameRoom tick.");
                return;
            }

            // Flush any chat messages that were queued while the chat panel wasn't available yet
            // (e.g. sent from a Photon callback while still on the MultiplayerMenu screen).
            if (pendingRoomChatMessages.Count > 0)
            {
                string[] toSend = pendingRoomChatMessages.ToArray();
                pendingRoomChatMessages.Clear();
                foreach (string msg in toSend)
                {
                    SendChatMessage(msg);
                }
            }

            if (roomCreatedTime == DateTime.MinValue || roomCreatedTime == DateTime.MaxValue)
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Entered GameRoom. Starting room timer.");
                LogEvent("room_entered");
                // lifecycle-event-logging.md: JSONL twin. The enclosing branch is entered on both
                // sentinels; MinValue means a genuinely new room (bot startup / post-disconnect
                // recreate, set by Plugin.Navigation's leave paths and PhotonContainerPrefix's
                // disconnect branch), MaxValue means the timer was frozen for an in-place settings
                // update. Must be evaluated BEFORE the assignment below clears the sentinel.
                LogJsonEvent("room_entered", ("fresh", roomCreatedTime == DateTime.MinValue));
                roomCreatedTime = DateTime.Now;
                lastActivityTime = DateTime.UtcNow;
                chatWarnedAboutNextRace = false;
                firstStartGameClickTime = DateTime.MinValue;

                // player-onboarding-ux.md work item 2: one short greeting per room entry (this
                // block only runs on a genuine new-room transition -- see roomCreatedTime's other
                // writers across the plugin -- never on a per-track/per-player basis), so anyone
                // in the room learns the plugin is present and how to find its commands without
                // having to ask. Same tag idiom as the "Up next"/self-disable SYSTEM messages.
                SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} LiftoffAutoLobby {FormatVariable(PluginVersion.Number)} is active here — type {FormatHighlight("/help")} for commands.");

                // Apply a persisted max-players override (survives bot restarts), if one is configured.
                string maxPlayersPath = Path.Combine(pluginPath, "max_players.txt");
                if (File.Exists(maxPlayersPath))
                {
                    int configuredMax;
                    if (int.TryParse(File.ReadAllText(maxPlayersPath).Trim(), out configuredMax))
                    {
                        int applied;
                        string err;
                        if (!SetRoomMaxPlayers(configuredMax, out applied, out err))
                        {
                            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to apply persisted max_players.txt ({configuredMax}): {err}");
                        }
                    }
                }
            }

            // democracy-skip.md: prune skip votes from players who left the room, then
            // re-evaluate the majority (a departure can lower the required threshold enough
            // for the remaining votes to now win). No-ops when democracy mode is off.
            SkipCommand.CheckDisconnectedVoters();

            double elapsed = (DateTime.Now - roomCreatedTime).TotalSeconds;
            ProcessClientScript(elapsed);

            // Auto-start: click the START button 15 seconds after entering the room to give players time to join
            if (GetAutoStart() && elapsed >= 15.0 && (DateTime.Now - lastStartGameClickedTime).TotalSeconds > 30.0)
            {
                string[] startNames = { "buttonStartGame", "btnStartGame", "StartGame", "btnStart", "buttonStart" };
                Button startBtn = FindButtonByTextOrName("START GAME", startNames);
                if (startBtn == null) startBtn = FindButtonByTextOrName("START", startNames);
                if (startBtn != null && startBtn.gameObject.activeInHierarchy && startBtn.interactable)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Auto-start: clicking START button.");
                    lastStartGameClickedTime = DateTime.Now;
                    if (firstStartGameClickTime == DateTime.MinValue)
                    {
                        firstStartGameClickTime = DateTime.Now;
                    }
                    startBtn.onClick.Invoke();
                    return;
                }
            }

            // Runtime watchdog: if Start Game has been clicked repeatedly but the scene never
            // transitioned into a flight level (race never actually loaded — the real-world
            // "Drawing Board" failure mode), treat this track as a runtime failure: blacklist it
            // for the rest of this session and recover to a known-good state. This is
            // defense-in-depth alongside the pre-launch validation, since a track can pass every
            // static check and still fail only when the race actually starts.
            if (firstStartGameClickTime != DateTime.MinValue)
            {
                double sinceFirstClick = (DateTime.Now - firstStartGameClickTime).TotalSeconds;
                if (sinceFirstClick > RaceLoadTimeoutSeconds)
                {
                    string failKey = $"{targetEnvironment}|{targetTrackName}|{targetGameMode}";
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] RUNTIME FAILURE: race did not load {sinceFirstClick:F0}s after Start Game click for '{targetTrackName}' (Env: {targetEnvironment}, Mode: {targetGameMode}). Blacklisting for this session.");
                    sessionBlacklistedTracks.Add(failKey);
                    // Structured JSON file event (A3): an autonomous plugin decision — a track that
                    // failed to load at runtime is blacklisted for the rest of the session.
                    LogJsonEvent("decision",
                        ("kind", "track_blacklist"),
                        ("detail", $"{targetEnvironment} - {targetTrackName} ({targetGameMode}) failed to load {sinceFirstClick:F0}s after Start Game"));
                    firstStartGameClickTime = DateTime.MinValue;
                    NavigateToMainMenu();
                    return;
                }
            }

            // Warn about the next track 10 seconds before rotation
            if (elapsed >= GetRotationInterval() - 15.0 && elapsed < GetRotationInterval())
            {
                if (DateTime.Now.Second % 5 == 0)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Close to rotation. chatWarned={chatWarnedAboutNextRace}, elapsed={elapsed:F1}s, target={GetRotationInterval() - 10.0:F1}s");
                }
            }

            AnnounceCalloutTiersIfDue(elapsed);
            AnnounceNextTrackIfDue(elapsed);

            if (skipRequested || elapsed >= GetRotationInterval())
            {
                if (skipRequested)
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Skip requested by admin — forcing rotation.");
                skipRequested = false;
                chatWarnedAboutNextRace = false;

                // Timer expired (or skip forced) inside waiting room, open change settings popup!
                string[] changeSettingsNames = { "buttonChangeRoomSettings", "btnChangeRoomSettings", "ChangeRoomSettings" };
                Button changeSettingsBtn = FindButtonByTextOrName("CHANGE SETTINGS", changeSettingsNames);
                if (changeSettingsBtn != null && changeSettingsBtn.gameObject.activeInHierarchy && changeSettingsBtn.interactable)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Clicking CHANGE SETTINGS button.");
                    changeSettingsBtn.onClick.Invoke();
                    roomCreatedTime = DateTime.MaxValue; // Freeze timer until settings updated
                }
                return;
            }

            HandleKeepAlive();
        }

        // ---------------------------------------------------------------
        // client-ingame-track-change.md (Plan B'): CLIENT-ONLY in-flight rotation. Server mode
        // never reaches any of the members below; HandleFlightLevel (the server's
        // exit-to-waiting-room path) is untouched.
        // ---------------------------------------------------------------

        // Room custom properties snapshotted immediately before an in-flight Update click, diffed
        // against the live room afterwards to confirm the apply landed (AGENTS.md rules 2-3;
        // VERDICT "Effect confirmation"). null means "unknown", never "changed".
        private static string inFlightPropsBeforeApply = null;
        // When the in-flight popup was requested, so a popup that never appears times out instead
        // of freezing rotation for the rest of the session.
        private static DateTime inFlightPopupRequestedTime = DateTime.MinValue;
        private const double InFlightPopupAppearTimeoutSeconds = 15.0;

        // Pre-rotation "Up next" callout. Extracted VERBATIM out of HandleGameRoom (pure move, no
        // rewording — client-chat-presentation.md owns the message text) so the client in-flight
        // rotation path can fire the same callout: conductor ruling 2026-07-26, from the
        // operator's "callouts are client-critical" decision — an in-flight track change must be
        // announced to the room exactly like a waiting-room one.
        private static void AnnounceNextTrackIfDue(double elapsed)
        {
            if (!chatWarnedAboutNextRace && elapsed >= GetRotationInterval() - 10.0 && elapsed < GetRotationInterval())
            {
                chatWarnedAboutNextRace = true;
                string nextEnv, nextMode;
                int trackIdx;
                string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx);
                if (!string.IsNullOrEmpty(nextTrackName))
                {
                    if (IsClientMode)
                    {
                        string body = RenderClientTemplate(Settings.UpNextTemplate, "Up next: {environment} - {track}",
                            ("track", nextTrackName ?? ""), ("environment", nextEnv ?? ""));
                        SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} {body}");
                    }
                    else
                    {
                        SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} Up next: {FormatHighlight($"{nextEnv} - {nextTrackName}")}");
                    }
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] PeekNextTrackName returned null/empty.");
                }
            }
        }

        // ---------------------------------------------------------------
        // pre-rotation-callouts.md: tiered pre-rotation warnings (5m/2m/1m by default),
        // extending the mechanism above rather than building a parallel one. The fixed final
        // 10s warning (AnnounceNextTrackIfDue/chatWarnedAboutNextRace, above) is untouched.
        // ---------------------------------------------------------------

        // Which configured offsets have already announced for the CURRENT approach to
        // rotation. Deliberately self-correcting instead of an explicit reset flag: every
        // entry is dropped again the moment `elapsed` is no longer past its threshold (a new
        // track started and elapsed fell back near zero, or /extend pushed elapsed back below
        // it), so it can fire again next time its threshold is crossed. This sidesteps needing
        // a reset call at every one of chatWarnedAboutNextRace's existing reset sites -- several
        // of which live in Plugin.cs / Plugin.RoomSetup.cs, outside this feature's file
        // partition (see the feature doc's "Design decisions" section) -- without adding a
        // second state mechanism that must be kept in sync (AGENTS.md rule 4: this is in-memory
        // per-tick bookkeeping, not a second persisted/derived state file).
        private static readonly HashSet<int> calloutOffsetsFired = new HashSet<int>();

        // Parses Settings.CalloutOffsetsMinutes ("5,2,1" by default) into whole seconds,
        // largest (earliest warning) first. Unparseable/non-positive entries are dropped
        // silently; an empty or fully-invalid result falls back to the 5/2/1 default so a
        // typo'd cfg value never silences every callout.
        private static List<int> GetCalloutOffsetsSeconds()
        {
            string raw = Settings.CalloutOffsetsMinutes;
            var result = new List<int>();
            if (!string.IsNullOrWhiteSpace(raw))
            {
                foreach (var part in raw.Split(','))
                {
                    double minutes;
                    if (double.TryParse(part.Trim(), System.Globalization.NumberStyles.Float,
                            System.Globalization.CultureInfo.InvariantCulture, out minutes) && minutes > 0)
                    {
                        int seconds = (int)Math.Round(minutes * 60.0);
                        if (seconds > 0 && !result.Contains(seconds)) result.Add(seconds);
                    }
                }
            }
            if (result.Count == 0) result.AddRange(new[] { 300, 120, 60 }); // 5m, 2m, 1m
            result.Sort((a, b) => b.CompareTo(a));
            return result;
        }

        // "300" -> "5m", "90" -> "1m30s", "45" -> "45s". Kept simple on purpose: the default
        // offsets are always whole minutes, this only has to look sane for a hand-edited cfg.
        private static string FormatOffsetLabel(int seconds)
        {
            if (seconds < 60) return $"{seconds}s";
            if (seconds % 60 == 0) return $"{seconds / 60}m";
            return $"{seconds / 60}m{seconds % 60}s";
        }

        // Called every tick alongside AnnounceNextTrackIfDue, from both the waiting-room path
        // (HandleGameRoom) and the client in-flight path (HandleClientInFlightRotation) -- same
        // callout, same two call sites, per the operator's "callouts are client-critical"
        // decision that extracted AnnounceNextTrackIfDue in the first place.
        private static void AnnounceCalloutTiersIfDue(double elapsed)
        {
            double intervalSecs = GetRotationInterval();
            foreach (int offsetSeconds in GetCalloutOffsetsSeconds())
            {
                // Acceptance criterion: an offset at or beyond the configured rotation interval
                // is skipped cleanly (no negative-time threshold, no spam). Also drops any stale
                // "fired" mark, in case the interval shrank at runtime.
                if (offsetSeconds >= intervalSecs)
                {
                    calloutOffsetsFired.Remove(offsetSeconds);
                    continue;
                }

                double threshold = intervalSecs - offsetSeconds;
                if (elapsed < threshold)
                {
                    calloutOffsetsFired.Remove(offsetSeconds); // not due yet, or re-armed by /extend
                    continue;
                }
                if (elapsed >= intervalSecs) continue; // rotation itself is due; HandleGameRoom takes over
                if (!calloutOffsetsFired.Add(offsetSeconds)) continue; // already announced this approach

                SendCalloutMessage(offsetSeconds);
            }
        }

        private static void SendCalloutMessage(int offsetSeconds)
        {
            string nextEnv, nextMode;
            int trackIdx;
            string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx);
            if (string.IsNullOrEmpty(nextTrackName))
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] AnnounceCalloutTiersIfDue: PeekNextTrackName returned null/empty.");
                return;
            }

            string timeLabel = FormatOffsetLabel(offsetSeconds);
            if (IsClientMode)
            {
                string body = RenderClientTemplate(Settings.CalloutTemplate, "Up next in {time}: {environment} - {track}",
                    ("track", nextTrackName ?? ""), ("environment", nextEnv ?? ""), ("time", timeLabel));
                SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} {body}");
            }
            else
            {
                SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} Up next in {FormatVariable(timeLabel)}: {FormatHighlight($"{nextEnv} - {nextTrackName}")}");
            }

            LogEvent("pre_rotation_callout", ("offset_seconds", offsetSeconds.ToString()), ("track", nextTrackName), ("environment", nextEnv ?? ""));
        }

        // CLIENT-ONLY in-flight rotation (Plan B', code item 4). Called from HandleClientTick when
        // the player is inside a flight level and rotation is engaged. Server mode never calls this
        // — the server keeps its HandleFlightLevel exit-to-waiting-room path, untouched.
        //
        // Request half (this commit): on rotation-interval expiry (or an admin /skip), fire the same
        // pre-rotation "Up next" callout the waiting-room path fires, ask InGameMenuMainPanel to
        // instantiate the multiplayer settings popup, and record that a request is outstanding. The
        // pickup/apply/confirm half runs on subsequent ticks (next micro-step).
        //
        // /pause and /stop keep working with no extra gate: /stop makes IsRotationEngaged() false so
        // HandleClientTick never reaches here, and /pause holds roomCreatedTime at DateTime.MaxValue
        // (MaintainRotationPauseFreeze), which this method treats as "timer not running".
        private static void HandleClientInFlightRotation()
        {
            if (!IsClientMode) return; // defense in depth: server mode must never reach this path

            if (inFlightPopupRequestedTime != DateTime.MinValue)
            {
                DriveInFlightSettingsPopup();
                return;
            }

            // DateTime.MaxValue = timer frozen (paused, or an apply in progress); DateTime.MinValue
            // = never started. Neither is a running rotation timer.
            if (roomCreatedTime == DateTime.MaxValue || roomCreatedTime == DateTime.MinValue) return;

            double elapsed = (DateTime.Now - roomCreatedTime).TotalSeconds;

            // Same callouts as the waiting-room path, same text: an in-flight track change is
            // announced to the room exactly like a waiting-room one (conductor ruling 2026-07-26
            // from the operator's "callouts are client-critical" decision).
            AnnounceCalloutTiersIfDue(elapsed);
            AnnounceNextTrackIfDue(elapsed);

            if (!skipRequested && elapsed < GetRotationInterval()) return;

            if (skipRequested)
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Client in-flight rotation: skip requested by admin — forcing rotation.");
            skipRequested = false;
            chatWarnedAboutNextRace = false;

            // Photon's InRoom is the scene-independent in-room test (there is no "GameRoom" object
            // in a flight scene). Nothing to update if the player is not in a room.
            if (!IsPhotonInRoom())
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Client in-flight rotation: interval expired but Photon reports not in a room — skipping this rotation.");
                roomCreatedTime = DateTime.Now; // retry a full interval from now, not every tick
                return;
            }

            if (!TryOpenInFlightMultiplayerSettingsPopup())
            {
                // Already logged by the helper. currentTrackName is deliberately left untouched:
                // nothing changed, so nothing may be reported as changed (AGENTS.md rule 2).
                roomCreatedTime = DateTime.Now; // retry on the next interval instead of hammering
                return;
            }

            // Request is out. Freeze the rotation timer until the apply resolves (the same freeze
            // the waiting-room path uses when it clicks CHANGE SETTINGS), and reset the popup-
            // driving flags so the pickup half sees a clean first sighting.
            inFlightPopupRequestedTime = DateTime.Now;
            inFlightPropsBeforeApply = null;
            popupWasOpen = false;
            isSubmittingSettings = false;
            roomCreatedTime = DateTime.MaxValue;
        }

        // Pickup / apply / confirm half of the client in-flight rotation (Plan B', code item 4).
        // Runs on every tick between the OnMultiplayerGameSettings() request and the resolution of
        // the apply. Three outcomes, all of which end with the request state cleared and the
        // rotation timer running again:
        //   1. popup appears -> ApplyRoomSettingsPopup(popup, allowCreate:false) drives it (the same
        //      live-proven driver server rotation uses; it clicks Update, never Create) -> once the
        //      popup closes, confirm the effect and log honestly either way.
        //   2. popup never appears within InFlightPopupAppearTimeoutSeconds -> warn, give up on this
        //      rotation, retry on the next interval. currentTrackName untouched.
        //   3. the player left the room mid-attempt -> abandon quietly.
        private static void DriveInFlightSettingsPopup()
        {
            PopupQuickPlayMultiplayerSetup popup = GameObject.FindObjectOfType<PopupQuickPlayMultiplayerSetup>();
            bool popupOpen = (popup != null && popup.gameObject.activeInHierarchy);

            if (popupOpen)
            {
                if (!popupWasOpen)
                {
                    // First sighting: same initialization the waiting-room client path does on
                    // popup open, so ApplyRoomSettingsPopup sees exactly the inputs it expects.
                    popupOpenedTime = DateTime.Now;
                    isSubmittingSettings = false;
                    triedCustomContentTab = false;
                    targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode);
                    popupWasOpen = true;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Client in-flight rotation: settings popup is up; target '{targetEnvironment} - {targetTrackName}' ({targetGameMode}).");
                }

                if (isSubmittingSettings) return; // Update already clicked — wait for the popup to close

                if ((DateTime.Now - popupOpenedTime).TotalSeconds < 2.0) return; // let it initialize

                // Snapshot the room's custom properties immediately BEFORE the first drive attempt,
                // so the post-apply diff has a valid baseline (AGENTS.md rules 2-3: confirm by
                // observed effect, never by "the call didn't throw"). Taken once per request.
                if (inFlightPropsBeforeApply == null)
                {
                    inFlightPropsBeforeApply = GetRoomPropertiesSnapshot();
                }

                // Keeps the player's own room name/visibility a deliberate no-op on Update.
                SyncClientRoomIdentityForPopup();
                ApplyRoomSettingsPopup(popup, allowCreate: false);
                return;
            }

            // Popup is not open.
            if (popupWasOpen)
            {
                // It was open and is now gone — the apply resolved (or the popup was dismissed).
                ConfirmInFlightApply();
                ClearInFlightRotationRequest(restartTimer: true);
                return;
            }

            if (!IsPhotonInRoom())
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Client in-flight rotation: no longer in a room while waiting for the settings popup — abandoning this rotation.");
                ClearInFlightRotationRequest(restartTimer: true);
                return;
            }

            double waited = (DateTime.Now - inFlightPopupRequestedTime).TotalSeconds;
            if (waited >= InFlightPopupAppearTimeoutSeconds)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Client in-flight rotation: settings popup never appeared {waited:F1}s after OnMultiplayerGameSettings — giving up on this rotation, the track was NOT changed. Retrying on the next interval.");
                ClearInFlightRotationRequest(restartTimer: true);
            }
        }

        // Effect confirmation for an in-flight apply (VERDICT "Effect confirmation", AGENTS.md
        // rules 2-3). Never claims success from a return value: the room's custom properties are
        // the actual transport, and the loaded-track read-back is the independent second opinion.
        // A null "before" snapshot means the baseline read failed — that is reported as unknown,
        // never as success.
        private static void ConfirmInFlightApply()
        {
            string after = GetRoomPropertiesSnapshot();
            string loadedTrack, loadedEnv;
            bool haveTrack = TryGetCurrentLoadedTrack(out loadedTrack, out loadedEnv);
            string trackDetail = haveTrack ? $"{loadedEnv} - {loadedTrack}" : "unreadable";

            if (inFlightPropsBeforeApply == null || after == null)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Client in-flight rotation: could not read room properties before/after the apply — result UNKNOWN (loaded track now: {trackDetail}).");
                return;
            }

            if (after != inFlightPropsBeforeApply)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Client in-flight rotation: room properties changed — apply CONFIRMED (loaded track now: {trackDetail}, target was '{targetEnvironment} - {targetTrackName}').");
            }
            else
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Client in-flight rotation: room properties are unchanged after the settings popup closed — the track was probably NOT changed (loaded track: {trackDetail}, target was '{targetEnvironment} - {targetTrackName}').");
            }
        }

        // Clears the outstanding in-flight request and (by default) restarts the rotation timer that
        // the request half froze at DateTime.MaxValue, so rotation keeps running whichever way the
        // attempt ended. ApplyRoomSettingsPopup already restarts the timer when it clicks Update;
        // this makes the failure paths behave the same instead of freezing rotation for the session.
        private static void ClearInFlightRotationRequest(bool restartTimer)
        {
            inFlightPopupRequestedTime = DateTime.MinValue;
            inFlightPropsBeforeApply = null;
            popupWasOpen = false;
            isSubmittingSettings = false;
            if (restartTimer && roomCreatedTime == DateTime.MaxValue)
            {
                roomCreatedTime = DateTime.Now;
                lastActivityTime = DateTime.UtcNow;
                chatWarnedAboutNextRace = false;
            }
        }

        private static void HandleFlightLevel()
        {
            double elapsed = (DateTime.Now - sceneLoadTime).TotalSeconds;
            if (elapsed >= 5.0)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Level loaded for {elapsed:F1}s. Exiting flight level to waiting room.");

                // 1. Try invoking InGameMenuMainPanel.OnToWaitingRoom directly via reflection
                try
                {
                    Type panelType = Type.GetType("InGameMenuMainPanel, Assembly-CSharp");
                    if (panelType != null)
                    {
                        var panels = Resources.FindObjectsOfTypeAll(panelType);
                        if (panels.Length > 0 && panels[0] != null)
                        {
                            var method = panelType.GetMethod("OnToWaitingRoom", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                            if (method != null)
                            {
                                UnityEngine.Debug.Log("[AutoLobbyPlugin] Found InGameMenuMainPanel! Invoking OnToWaitingRoom() programmatically.");
                                method.Invoke(panels[0], null);
                                return;
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to programmatically exit via InGameMenuMainPanel: {ex.Message}");
                }

                // 2. Fallback to finding the button in hierarchy and clicking it
                Button toWaitingRoomBtn = null;
                Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
                foreach (var btn in buttons)
                {
                    if (btn != null && btn.name == "btnToWaitingRoom")
                    {
                        toWaitingRoomBtn = btn;
                        break;
                    }
                }

                if (toWaitingRoomBtn != null)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Found btnToWaitingRoom. Invoking click!");
                    toWaitingRoomBtn.onClick.Invoke();
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] btnToWaitingRoom not found in hierarchy.");
                }
            }
        }

        private static readonly string[] DefaultProTips = new string[]
        {
            "High camera tilt (e.g., 30°+) is ideal for fast forward flight but makes landing and hovering harder.",
            "Increase your camera FOV to improve peripheral vision and awareness of obstacles.",
            "If your drone feels loose in corners, try slightly increasing your Pitch/Roll D gains.",
            "Pro tip: To get a faster lap time, simply fly faster.",
            "Avoid hitting trees. They do not move, and they will win every fight.",
            "Remember: Gravity is not just a suggestion, it is the law."
        };

        private static string GetRandomProTip()
        {
            try
            {
                string path = Path.Combine(pluginPath, "pro_tips.txt");
                if (File.Exists(path))
                {
                    List<string> lines = new List<string>();
                    foreach (var line in File.ReadAllLines(path))
                    {
                        string trimmed = line.Trim();
                        if (!string.IsNullOrEmpty(trimmed) && !trimmed.StartsWith("#"))
                        {
                            lines.Add(trimmed);
                        }
                    }
                    if (lines.Count > 0)
                    {
                        int index = rng.Next(0, lines.Count);
                        return lines[index];
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error loading pro_tips.txt: {ex.Message}");
            }

            int fallbackIndex = rng.Next(0, DefaultProTips.Length);
            return DefaultProTips[fallbackIndex];
        }

        private static void HandleKeepAlive()
        {
            try
            {
                double interval = GetKeepAliveInterval();
                if ((DateTime.UtcNow - lastActivityTime).TotalSeconds >= interval)
                {
                    string tip = GetRandomProTip();
                    SendChatMessage($"{FormatTag("PRO TIP", activeTheme.welcomeTagColor)} {tip}");
                    lastActivityTime = DateTime.UtcNow;
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in HandleKeepAlive: {ex}");
            }
        }

        private static void CancelMaintenance()
        {
            maintenanceActive = false;
            maintenanceTime = DateTime.MaxValue;
            lastMaintenanceWarningMinutes = -1;
            maintenanceWarning30sSent = false;
            maintenanceWarning10sSent = false;
            try
            {
                string path = Path.Combine(pluginPath, "maintenance_active.txt");
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to delete maintenance_active.txt: {ex.Message}");
            }
        }
    }
}
