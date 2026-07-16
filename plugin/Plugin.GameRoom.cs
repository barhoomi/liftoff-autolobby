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
                roomCreatedTime = DateTime.Now;
                lastActivityTime = DateTime.UtcNow;
                chatWarnedAboutNextRace = false;
                firstStartGameClickTime = DateTime.MinValue;

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

            if (!chatWarnedAboutNextRace && elapsed >= GetRotationInterval() - 10.0 && elapsed < GetRotationInterval())
            {
                chatWarnedAboutNextRace = true;
                string nextEnv, nextMode;
                int trackIdx;
                string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx);
                if (!string.IsNullOrEmpty(nextTrackName))
                {
                    SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} Up next: {FormatHighlight($"{nextEnv} - {nextTrackName}")}");
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] PeekNextTrackName returned null/empty.");
                }
            }

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
