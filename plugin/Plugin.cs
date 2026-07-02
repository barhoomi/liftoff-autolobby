using System;
using System.IO;
using System.Text;
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
    [BepInPlugin("com.lugus.liftoff.autolobby", "Liftoff Auto Lobby", "1.0.0")]
    public class AutoLobbyPlugin : BaseUnityPlugin
    {
        private static string pluginPath;
        private static DateTime lastTickTime = DateTime.MinValue;

        // Rotation & State Management
        private static DateTime roomCreatedTime = DateTime.MinValue;
        private static DateTime popupOpenedTime = DateTime.MinValue;
        private static DateTime popupSubmittedTime = DateTime.MinValue;
        private static DateTime lastStartGameClickedTime = DateTime.MinValue;
        private static bool popupWasOpen = false;
        private static bool isSubmittingSettings = false;
        private static string targetTrackName = "";
        private static string targetEnvironment = "";
        private static string targetGameMode = "";
        private static string targetLobbyName = "";
        private static bool isLeaving = false;

        private static bool chatWarnedAboutNextRace = false;
        private static bool liftoffProLoginAttempted = false;
        private static DateTime liftoffProLoginClickTime = DateTime.MinValue;
        private static DateTime lastSignInClickTime = DateTime.MinValue;
        private static bool signInClickAttempted = false; // limits MultiplayerMenu sign-in to exactly one click per appearance (reduce-login-retry-attempts)
        private static bool signInWasVisible = false;      // tracks appearance transitions so signInClickAttempted resets per-appearance, not per-tick
        private static bool triedCustomContentTab = false;
        private static bool steamStatusLogged = false;
        private static string lastSceneName = "";
        private static DateTime sceneLoadTime = DateTime.MinValue;
        private static DateTime lastInRoomTime = DateTime.MinValue;
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
        // Runtime failure handling: tracks that passed pre-launch validation but never actually
        // loaded a race after Start Game was clicked. In-memory only — cleared on process restart.
        private static HashSet<string> sessionBlacklistedTracks = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private static DateTime firstStartGameClickTime = DateTime.MinValue;
        private const double RaceLoadTimeoutSeconds = 60.0;
        private static List<Tuple<string, string, DateTime>> processedMessages = new List<Tuple<string, string, DateTime>>();

        // Admin
        private static HashSet<string> adminIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private static bool skipRequested = false;
        private static bool shuffleMode = false;
        private static System.Random rng = new System.Random();
        private static bool maintenanceActive = false;
        private static DateTime maintenanceTime = DateTime.MaxValue;
        private static int lastMaintenanceWarningMinutes = -1;
        private static bool maintenanceWarning30sSent = false;
        private static bool maintenanceWarning10sSent = false;

        // Room visibility / max players / rename-via-recreate flow
        private static bool pendingPrivateRoomRename = false;   // true while a /private <name> request is in flight
        private static DateTime pendingPrivateRoomRenameStartTime = DateTime.MinValue; // global watchdog: aborts the whole rename if it never reaches a create/join attempt at all (e.g. an unrelated sign-in screen derails navigation before ConfigureAndCreateRoom ever runs)
        private static string pendingPrivateRoomName = "";
        private static string pendingPrivateRoomAdmin = "";
        private static bool pendingJoinByName = false;          // set by OnCreateRoomFailed(GameIdAlreadyExists), consumed in MultiplayerMenu lobby list
        private static DateTime pendingJoinByNameSetTime = DateTime.MinValue; // hard timeout for the whole join-by-name flow, regardless of which step gets stuck
        private static DateTime joinByNameButtonClickedTime = DateTime.MinValue;
        private static bool joinByNamePanelSubmitted = false;
        private static bool roomOwnedByBot = true;              // false once the bot joins a room it did not create (no control over settings/rotation)
        // Confirmed via live UI dump (see ProcessJoinByNameFlow): GameObject name is "InputFieldName".
        // Shared so the leftover-panel self-correction check in HandleMultiplayerMenu can't drift
        // from the names ProcessJoinByNameFlow actually matches against.
        private static readonly string[] JoinByNameRoomFieldNames = { "InputFieldName", "fieldRoomName", "inputFieldRoomName", "RoomName" };

        // Messages queued from Photon-callback context (create/join/master-switch handlers) that
        // may fire while still on the MultiplayerMenu screen, before the in-room chat panel exists.
        // Sending immediately there reliably throws ("chatPanel activeInHierarchy: False") and the
        // message is lost. Flushed every GameRoom tick once the panel is actually available.
        private static readonly List<string> pendingRoomChatMessages = new List<string>();

        private static void QueueChatMessage(string message)
        {
            if (string.IsNullOrEmpty(message)) return;
            pendingRoomChatMessages.Add(message);
        }

        private void Awake()
        {
            Logger.LogInfo("[AutoLobbyPlugin] BepInEx Awake called!");
            try
            {
                pluginPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "BepInEx", "plugins");

                LoadAdminIds();

                // Load initial shuffle mode
                shuffleMode = GetShuffleMode();
                Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial shuffleMode: {shuffleMode}");

                // Apply Harmony patches to fix database loading exceptions
                ApplyHarmonyPatches();

                // Dynamically resolve multiplayer client connection check method
                ResolveMultiplayerClientCheck();

                // Subscribe to the static Canvas render event (runs on main thread every frame)
                Canvas.willRenderCanvases += OnWillRenderCanvases;
                
                Logger.LogInfo("[AutoLobbyPlugin] Static Canvas.willRenderCanvases hook registered successfully!");
            }
            catch (Exception ex)
            {
                Logger.LogError($"[AutoLobbyPlugin] Static hook registration failed: {ex.Message}");
            }
        }

        private static void LoadAdminIds()
        {
            adminIds.Clear();
            string adminPath = Path.Combine(pluginPath, "admin_ids.txt");
            if (File.Exists(adminPath))
            {
                foreach (var line in File.ReadAllLines(adminPath))
                {
                    string id = line.Trim();
                    if (!string.IsNullOrEmpty(id) && !id.StartsWith("#"))
                        adminIds.Add(id);
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded {adminIds.Count} admin ID(s).");
            }
            else
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] admin_ids.txt not found — no admins configured.");
            }
        }

        private static bool IsAdmin(string userId) => adminIds.Contains(userId);

        private static void OnWillRenderCanvases()
        {
            try
            {
                // Run our tick logic every 1.0 seconds to keep overhead low
                if ((DateTime.Now - lastTickTime).TotalSeconds < 1.0)
                    return;
                lastTickTime = DateTime.Now;

                RunTick();
            }
            catch (Exception ex)
            {
                // Catch all to ensure we never crash Unity's canvas rendering loop
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in OnWillRenderCanvases: {ex}");
            }
        }

        private void Update()
        {
            try
            {
                // Run our tick logic every 1.0 seconds to keep overhead low
                if ((DateTime.Now - lastTickTime).TotalSeconds < 1.0)
                    return;
                lastTickTime = DateTime.Now;

                RunTick();
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in Update: {ex}");
            }
        }

        private static void RunTick()
        {
            // 1. Check for external/internal maintenance mode
            try
            {
                string mPath = Path.Combine(pluginPath, "maintenance_active.txt");
                if (File.Exists(mPath))
                {
                    if (!maintenanceActive)
                    {
                        maintenanceActive = true;
                        maintenanceTime = DateTime.Now.AddMinutes(3.0);
                        lastMaintenanceWarningMinutes = -1;
                        maintenanceWarning30sSent = false;
                        maintenanceWarning10sSent = false;
                        SendChatMessage("<color=#0000FF>[ADMIN]</color> Shutdown for maintenance scheduled in <color=#00FF88><i>3.0m</i></color> (triggered externally).");
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode triggered externally.");
                    }
                }
                else
                {
                    if (maintenanceActive)
                    {
                        CancelMaintenance();
                        SendChatMessage("<color=#0000FF>[ADMIN]</color> Scheduled maintenance cancelled externally.");
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode cancelled externally.");
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error checking external maintenance file: {ex.Message}");
            }

            if (maintenanceActive)
            {
                double remainingSecs = (maintenanceTime - DateTime.Now).TotalSeconds;
                if (remainingSecs <= 0)
                {
                    SendChatMessage("<color=#0000FF>[ADMIN]</color> Going down for maintenance.");
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance time reached. Exiting game.");
                    Application.Quit();
                    return; // Prevent running other tick logic
                }
                else
                {
                    int remainingMinutes = (int)Math.Ceiling(remainingSecs / 60.0);
                    if (remainingMinutes > 0 && remainingMinutes != lastMaintenanceWarningMinutes && remainingSecs > 30.0)
                    {
                        lastMaintenanceWarningMinutes = remainingMinutes;
                        SendChatMessage($"<color=#0000FF>[ADMIN]</color> Shutdown for maintenance in <color=#00FF88><i>{remainingMinutes}m</i></color>.");
                    }
                    else if (remainingSecs <= 30.0 && !maintenanceWarning30sSent)
                    {
                        maintenanceWarning30sSent = true;
                        SendChatMessage("<color=#0000FF>[ADMIN]</color> Shutdown for maintenance in <color=#00FF88><i>30s</i></color>.");
                    }
                    else if (remainingSecs <= 10.0 && !maintenanceWarning10sSent)
                    {
                        maintenanceWarning10sSent = true;
                        SendChatMessage("<color=#0000FF>[ADMIN]</color> Shutdown for maintenance in <color=#00FF88><i>10s</i></color>.");
                    }
                }
            }

            if (!steamStatusLogged)
            {
                steamStatusLogged = true;
                try
                {
                    bool isRunning = Steamworks.SteamAPI.IsSteamRunning();
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] SteamAPI.IsSteamRunning(): {isRunning}");
                    try
                    {
                        if (Steamworks.SteamAPI.Init())
                        {
                            UnityEngine.Debug.Log("[AutoLobbyPlugin] SteamAPI.Init() returned True.");
                            string personaName = Steamworks.SteamFriends.GetPersonaName();
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Steam Persona Name: {personaName}");
                            ulong steamId = (ulong)Steamworks.SteamUser.GetSteamID();
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Steam ID: {steamId}");
                        }
                        else
                        {
                            UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] SteamAPI.Init() returned False.");
                        }
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception when calling SteamAPI.Init(): {ex.Message}");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to check Steam status: {ex.Message}");
                }
            }

            string sceneName = SceneManager.GetActiveScene().name;
            if (sceneName != lastSceneName)
            {
                lastSceneName = sceneName;
                sceneLoadTime = DateTime.Now;
                lastInRoomTime = DateTime.MinValue;
                sceneObjectsDumped = false;
                lastMenuStateDumpTime = DateTime.MinValue;
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Scene changed to: {sceneName}");

                // Reset room timer when loading into a flight level scene
                if (sceneName != "MainMenu" && sceneName != "MultiplayerMenu" &&
                    sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene")
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Level loaded. Resetting room timer.");
                    roomCreatedTime = DateTime.Now;
                    isLeaving = false;
                    firstStartGameClickTime = DateTime.MinValue; // race loaded successfully, disarm runtime watchdog
                }
            }

            // Log status every 30 seconds for visibility
            if (DateTime.Now.Second % 30 == 0)
            {
                double elapsed = roomCreatedTime != DateTime.MinValue ? (DateTime.Now - roomCreatedTime).TotalSeconds : 0;
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Tick running. Scene: {sceneName}, Room timer elapsed: {elapsed:F1}s / {GetRotationInterval()}s");
            }

            // Global safety net: the join-by-name flow's own internal timeouts (15s hard cap,
            // 10s field-lookup cap) only fire from inside ProcessJoinByNameFlow/HandleCreateRoomFailed
            // — if a /private <name> rename gets derailed onto an unrelated screen before ever
            // reaching a create/join attempt (e.g. leaving a room triggers a full Photon disconnect
            // that surfaces a sign-in prompt), none of those internal timeouts ever run, and the bot
            // can wander indefinitely. This fires regardless of which scene/panel it's stuck on.
            if (pendingPrivateRoomRename && pendingPrivateRoomRenameStartTime != DateTime.MinValue &&
                (DateTime.Now - pendingPrivateRoomRenameStartTime).TotalSeconds > 90.0)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Private room rename to '{pendingPrivateRoomName}' stuck for 90s+ with no create/join resolution — aborting and reloading MainMenu to recover.");
                pendingPrivateRoomRename = false;
                pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                pendingJoinByName = false;
                joinByNamePanelSubmitted = false;
                liftoffProLoginAttempted = false;
                liftoffProLoginClickTime = DateTime.MinValue;
                try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "false"); } catch { }
                QueueChatMessage($"<color=#0000FF>[ADMIN]</color> Room rename to '<color=#00FF88><i>{pendingPrivateRoomName}</i></color>' got stuck and was aborted — recovering with a public room.");
                SceneManager.LoadScene("MainMenu");
                return;
            }

            if ((DateTime.Now - popupSubmittedTime).TotalSeconds < 5.0)
            {
                return;
            }

            DismissPopups();

            // Check if settings popup is open globally (in MultiplayerMenu or Flight Level)
            PopupQuickPlayMultiplayerSetup popup = GameObject.FindObjectOfType<PopupQuickPlayMultiplayerSetup>();
            bool popupOpen = (popup != null && popup.gameObject.activeInHierarchy);

            if (popupOpen)
            {
                if (!popupWasOpen)
                {
                    popupOpenedTime = DateTime.Now;
                    isSubmittingSettings = false;
                    triedCustomContentTab = false;
                    // Read config files on popup open
                    targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode);
                    
                    string lobbyNamePath = Path.Combine(pluginPath, "lobby_name.txt");
                    if (File.Exists(lobbyNamePath))
                    {
                        targetLobbyName = File.ReadAllText(lobbyNamePath).Trim();
                    }
                    if (string.IsNullOrEmpty(targetLobbyName))
                    {
                        targetLobbyName = "Procedural Loop Room";
                    }

                    popupWasOpen = true;
                }

                // A create attempt just failed on a name collision — back out of this popup
                // instead of retrying Create with the same (still-taken) name, so the bot can
                // reach the lobby-list screen and drive the join-by-name fallback from there.
                if (pendingJoinByName && !joinByNamePanelSubmitted)
                {
                    isSubmittingSettings = false;
                    Button cancelBtn = GetPopupCancelButton(popup);
                    if (cancelBtn != null && cancelBtn.gameObject.activeInHierarchy && cancelBtn.interactable)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Cancelling settings popup to pivot to join-by-name fallback.");
                        cancelBtn.onClick.Invoke();
                    }
                    return;
                }

                // If settings are already submitted, do not touch the UI elements again
                if (isSubmittingSettings)
                {
                    if (DateTime.Now.Second % 5 == 0)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Settings submitted, waiting for settings popup to close...");
                    }
                    return;
                }

                // Wait 2 seconds for settings popup to fully initialize
                double popupAge = (DateTime.Now - popupOpenedTime).TotalSeconds;
                if (popupAge < 2.0)
                {
                    if (DateTime.Now.Second % 5 == 0)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for settings popup to initialize (age: {popupAge:F1}s)...");
                    }
                    return;
                }

                ConfigureAndCreateRoom(popup);
                return;
            }
            else
            {
                if (popupWasOpen)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Settings popup closed.");
                    // If the room timer was frozen, unfreeze/reset it now that the popup is closed
                    if (roomCreatedTime == DateTime.MaxValue)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Unfreezing room timer (setting to DateTime.Now).");
                        roomCreatedTime = DateTime.Now;
                        chatWarnedAboutNextRace = false;
                    }
                    isSubmittingSettings = false;
                }
                popupWasOpen = false;
            }

            if (string.IsNullOrEmpty(sceneName)) return;

            if (sceneName == "MainMenu")
            {
                HandleMainMenu();
            }
            else if (sceneName == "MultiplayerMenu")
            {
                HandleMultiplayerMenu();
            }
            else if (sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene")
            {
                popupWasOpen = false;
                HandleFlightLevel();
            }
        }

        private static string GetButtonText(Button btn)
        {
            if (btn == null) return "";

            // 1. Try legacy Text component
            Text t = btn.GetComponentInChildren<Text>();
            if (t != null) return t.text;

            // 2. Try TextMeshPro components via reflection to avoid static references
            foreach (var comp in btn.GetComponentsInChildren<Component>(true))
            {
                if (comp == null) continue;
                string typeName = comp.GetType().Name;
                if (typeName.Equals("TextMeshProUGUI", StringComparison.OrdinalIgnoreCase) ||
                    typeName.Equals("TextMeshPro", StringComparison.OrdinalIgnoreCase) ||
                    typeName.Equals("TMP_Text", StringComparison.OrdinalIgnoreCase))
                {
                    var prop = comp.GetType().GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
                    if (prop != null)
                    {
                        object val = prop.GetValue(comp);
                        if (val != null) return val.ToString();
                    }
                }
            }

            return "";
        }

        private static Button FindButtonByTextOrName(string targetText, string[] targetNames)
        {
            Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
            
            // Pass 1: Active + Name Match
            if (targetNames != null)
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || !btn.gameObject.activeInHierarchy) continue;
                    foreach (string name in targetNames)
                    {
                        if (btn.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        {
                            return btn;
                        }
                    }
                }
            }

            // Pass 2: Active + Text Match
            if (!string.IsNullOrEmpty(targetText))
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || !btn.gameObject.activeInHierarchy) continue;
                    string txt = GetButtonText(btn);
                    if (!string.IsNullOrEmpty(txt) && txt.Equals(targetText, StringComparison.OrdinalIgnoreCase))
                    {
                        return btn;
                    }
                }
            }

            // Pass 3: Inactive + Name Match (fallback)
            if (targetNames != null)
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || btn.gameObject.activeInHierarchy) continue;
                    foreach (string name in targetNames)
                    {
                        if (btn.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        {
                            return btn;
                        }
                    }
                }
            }

            // Pass 4: Inactive + Text Match (fallback)
            if (!string.IsNullOrEmpty(targetText))
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || btn.gameObject.activeInHierarchy) continue;
                    string txt = GetButtonText(btn);
                    if (!string.IsNullOrEmpty(txt) && txt.Equals(targetText, StringComparison.OrdinalIgnoreCase))
                    {
                        return btn;
                    }
                }
            }

            return null;
        }

        private static bool PopupContainsText(GameObject popupCanvas, string needle)
        {
            foreach (var comp in popupCanvas.GetComponentsInChildren<Component>(true))
            {
                if (comp == null) continue;
                string text = null;
                Text legacyText = comp as Text;
                if (legacyText != null)
                {
                    text = legacyText.text;
                }
                else
                {
                    string typeName = comp.GetType().Name;
                    if (typeName.Equals("TextMeshProUGUI", StringComparison.OrdinalIgnoreCase) ||
                        typeName.Equals("TMP_Text", StringComparison.OrdinalIgnoreCase))
                    {
                        var prop = comp.GetType().GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
                        if (prop != null) text = prop.GetValue(comp)?.ToString();
                    }
                }
                if (!string.IsNullOrEmpty(text) && text.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
            return false;
        }

        private static void DismissPopups()
        {
            GameObject popupCanvas = GameObject.Find("PopupCanvas(Clone)");
            if (popupCanvas != null && popupCanvas.activeInHierarchy)
            {
                bool isTrackUnavailable = PopupContainsText(popupCanvas, "not shared") ||
                                          PopupContainsText(popupCanvas, "not available");

                Button[] buttons = popupCanvas.GetComponentsInChildren<Button>(true);
                foreach (Button btn in buttons)
                {
                    if (btn != null && btn.gameObject.activeInHierarchy && btn.interactable)
                    {
                        string txt = GetButtonText(btn);
                        if (!string.IsNullOrEmpty(txt) &&
                            (txt.Equals("Confirm", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("OK", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("Close", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("Got it", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("Dismiss", StringComparison.OrdinalIgnoreCase)))
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Auto-dismissing popup (trackUnavailable={isTrackUnavailable}). Button: {txt}");
                            btn.onClick.Invoke();

                            if (isTrackUnavailable)
                            {
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Track not shareable, skipping: {targetTrackName}");
                                targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode);
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Advanced to next track: {targetTrackName} ({targetEnvironment})");
                                isSubmittingSettings = false;
                            }
                            else if (isSubmittingSettings)
                            {
                                // Some other alert (e.g. "room already exists") appeared after a
                                // submit — don't leave the bot stuck waiting for a popup that will
                                // never close on its own.
                                UnityEngine.Debug.Log("[AutoLobbyPlugin] Dismissed an alert while a settings submission was in flight — resetting isSubmittingSettings so the bot can retry.");
                                isSubmittingSettings = false;
                            }
                            break;
                        }
                    }
                }
            }
        }

        private static Button FindLiftoffProSignInButton()
        {
            Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
            foreach (Button btn in buttons)
            {
                if (btn == null || !btn.gameObject.activeInHierarchy || !btn.interactable) continue;
                string txt = GetButtonText(btn);
                string name = btn.name ?? "";
                if (txt.IndexOf("liftoff pro", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("liftoffpro", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("LiftoffPro", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("SignInPro", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    name.IndexOf("BtnPro", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return btn;
                }
            }
            return null;
        }

        private static void HandleMainMenu()
        {
            // Reset rotation state
            roomCreatedTime = DateTime.MinValue;
            isLeaving = false;

            double timeSinceLoad = (DateTime.Now - sceneLoadTime).TotalSeconds;

            // Log all visible buttons every 5s so we can see what's on screen
            LogMultiplayerMenuState();

            // Wait 3s for the menu to fully render before doing anything
            if (timeSinceLoad < 3.0)
                return;

            // Step 1: Sign in with Liftoff Pro if we haven't yet this session
            if (!liftoffProLoginAttempted)
            {
                Button proBtn = FindLiftoffProSignInButton();
                if (proBtn != null)
                {
                    liftoffProLoginAttempted = true;
                    liftoffProLoginClickTime = DateTime.Now;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking Liftoff Pro sign-in button on MainMenu: name='{proBtn.name}' text='{GetButtonText(proBtn)}'");
                    proBtn.onClick.Invoke();
                    return;
                }
                else
                {
                    // No Liftoff Pro button found — already signed in, or button not present
                    if (DateTime.Now.Second % 10 == 0)
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] No Liftoff Pro sign-in button found on MainMenu — proceeding as already signed in.");
                    liftoffProLoginAttempted = true; // don't keep searching every tick
                }
            }

            // Step 2: If we just clicked the Pro sign-in button, wait up to 30s for it to complete
            if (liftoffProLoginAttempted && liftoffProLoginClickTime != DateTime.MinValue)
            {
                double elapsed = (DateTime.Now - liftoffProLoginClickTime).TotalSeconds;
                // Check if the button disappeared (sign-in completed / we moved past that state)
                Button proBtn = FindLiftoffProSignInButton();
                if (proBtn != null && elapsed < 30.0)
                {
                    if (DateTime.Now.Second % 5 == 0)
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for Liftoff Pro sign-in to complete ({elapsed:F0}s / 30s)...");
                    return;
                }
                // Button gone or timeout reached — proceed
                liftoffProLoginClickTime = DateTime.MinValue;
                if (elapsed >= 30.0)
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Liftoff Pro sign-in timed out after 30s — proceeding anyway.");
                else
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Liftoff Pro sign-in button is gone — sign-in likely completed.");
            }

            // Step 3: Navigate to Multiplayer — wait 5s total before navigating
            if (timeSinceLoad < 5.0)
                return;

            // 3a. Click the Lobby sub-button if already expanded
            string[] lobbyNames = { "MultiplayerLobby", "btnMultiplayerLobby" };
            Button lobbyBtn = FindButtonByTextOrName("LOBBY", lobbyNames);
            if (lobbyBtn != null && lobbyBtn.gameObject.activeInHierarchy && lobbyBtn.interactable)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking LOBBY button: {lobbyBtn.name}");
                lobbyBtn.onClick.Invoke();
                return;
            }

            // 3b. Expand the Multiplayer category first
            string[] categoryNames = { "BtnHeading", "Multiplayer" };
            Button categoryBtn = FindButtonByTextOrName("MULTIPLAYER", categoryNames);
            if (categoryBtn != null)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking MULTIPLAYER category button: {categoryBtn.name}");
                categoryBtn.onClick.Invoke();
            }
        }

        private static bool sceneObjectsDumped = false;
        private static DateTime lastMenuStateDumpTime = DateTime.MinValue;

        private static void LogMultiplayerMenuState()
        {
            if ((DateTime.Now - lastMenuStateDumpTime).TotalSeconds < 5.0) return;
            lastMenuStateDumpTime = DateTime.Now;

            UnityEngine.Debug.Log("[AutoLobbyPlugin] === MultiplayerMenu Active UI State ===");
            try
            {
                Button[] allButtons = Resources.FindObjectsOfTypeAll<Button>();
                int count = 0;
                foreach (Button btn in allButtons)
                {
                    if (btn == null || !btn.gameObject.activeInHierarchy) continue;
                    string txt = GetButtonText(btn);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin]  BUTTON name='{btn.name}' text='{txt}' interactable={btn.interactable}");
                    count++;
                }

                InputField[] allInputs = Resources.FindObjectsOfTypeAll<InputField>();
                foreach (InputField inp in allInputs)
                {
                    if (inp == null || !inp.gameObject.activeInHierarchy) continue;
                    string placeholder = "";
                    if (inp.placeholder != null)
                    {
                        Text pt = inp.placeholder as Text;
                        if (pt != null) placeholder = pt.text;
                    }
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin]  INPUT name='{inp.name}' placeholder='{placeholder}' hasContent={!string.IsNullOrEmpty(inp.text)}");
                }

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] === End State ({count} active buttons) ===");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] LogMultiplayerMenuState error: {ex.Message}");
            }
        }

        private static void DumpActiveSceneObjects()
        {
            if (sceneObjectsDumped) return;
            sceneObjectsDumped = true;
            UnityEngine.Debug.Log("[AutoLobbyPlugin] === DUMPING ACTIVE SCENE OBJECTS IN MultiplayerMenu ===");
            try
            {
                foreach (GameObject obj in Resources.FindObjectsOfTypeAll<GameObject>())
                {
                    if (obj != null && obj.activeInHierarchy)
                    {
                        // Print path to object
                        string path = obj.name;
                        Transform p = obj.transform.parent;
                        while (p != null)
                        {
                            path = p.name + "/" + path;
                            p = p.parent;
                        }
                        // If it has a Button component, print it
                        Button b = obj.GetComponent<Button>();
                        string buttonInfo = b != null ? $" [BUTTON: '{GetButtonText(b)}', interactable={b.interactable}]" : "";
                        UnityEngine.Debug.Log($"  - {path}{buttonInfo}");
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error dumping scene objects: {ex.Message}");
            }
            UnityEngine.Debug.Log("[AutoLobbyPlugin] =====================================================");
        }

        private static void NavigateToMainMenu()
        {
            // Prefer clicking the real QUIT/BACK button so the game's own nav stack stays clean
            string[] quitNames = { "buttonQuit", "btnQuit", "ButtonQuit", "BtnQuit", "buttonBack", "btnBack", "BackButton", "QuitButton" };
            Button quitBtn = FindButtonByTextOrName("QUIT", quitNames);
            if (quitBtn == null) quitBtn = FindButtonByTextOrName("BACK", quitNames);
            if (quitBtn == null) quitBtn = FindButtonByTextOrName("EXIT", null);

            if (quitBtn != null && quitBtn.gameObject.activeInHierarchy && quitBtn.interactable)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking QUIT/BACK button to return to MainMenu: {quitBtn.name}");
                quitBtn.onClick.Invoke();
            }
            else
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] QUIT/BACK button not found — falling back to SceneManager.LoadScene(MainMenu).");
                SceneManager.LoadScene("MainMenu");
            }
        }

        private static void HandleMultiplayerMenu()
        {
            DumpActiveSceneObjects();
            LogMultiplayerMenuState();

            // Self-correction: if the join-by-name sub-panel is active but we're not actually
            // driving a join-by-name flow right now, it's a leftover from an aborted flow (e.g. a
            // Photon disconnect mid-flow surfaced a sign-in screen and stranded this panel
            // underneath it — the reproduced 2026-07-02 incident). Reload the scene to force back
            // to the canonical lobby-list state instead of guessing at a "Back"/"Cancel" button
            // name for a panel whose real names have already fooled a decompiled-class guess once
            // (buttonJoinRoomByName vs. the real buttonJoinByName) — a fresh scene load destroys
            // the leftover panel outright and is safe here since nothing legitimate is in flight.
            bool expectedJoinByNameFlow = pendingJoinByName && !joinByNamePanelSubmitted;
            if (!expectedJoinByNameFlow)
            {
                InputField leftoverJoinField = FindInputFieldByName(JoinByNameRoomFieldNames, "game name");
                if (leftoverJoinField != null && leftoverJoinField.gameObject.activeInHierarchy)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Found leftover join-by-name panel active with no join-by-name flow in progress — reloading MultiplayerMenu to recover.");
                    SceneManager.LoadScene("MultiplayerMenu");
                    return;
                }
            }

            // If a sign-in screen is still showing here (Liftoff Pro didn't complete from MainMenu),
            // log it prominently and navigate back to MainMenu to retry sign-in there.
            bool signInVisible = false;
            foreach (Button btn in Resources.FindObjectsOfTypeAll<Button>())
            {
                if (btn == null || !btn.gameObject.activeInHierarchy) continue;
                string txt = GetButtonText(btn);
                if (string.IsNullOrEmpty(txt)) continue;
                bool isSignIn = txt.IndexOf("sign in", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                txt.IndexOf("log in", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                txt.IndexOf("login", StringComparison.OrdinalIgnoreCase) >= 0;
                bool isSkip = txt.IndexOf("skip", StringComparison.OrdinalIgnoreCase) >= 0 ||
                              txt.IndexOf("guest", StringComparison.OrdinalIgnoreCase) >= 0 ||
                              txt.IndexOf("anonymous", StringComparison.OrdinalIgnoreCase) >= 0 ||
                              txt.IndexOf("without", StringComparison.OrdinalIgnoreCase) >= 0;
                if (isSignIn && !isSkip)
                {
                    signInVisible = true;
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Sign-in button still visible in MultiplayerMenu: name='{btn.name}' text='{txt}'");
                }
            }

            if (signInVisible)
            {
                if (!signInWasVisible)
                {
                    signInWasVisible = true;
                    signInClickAttempted = false;
                }

                double timeSinceLoad = (DateTime.Now - sceneLoadTime).TotalSeconds;

                // Wait 5s for the UI to fully settle before clicking anything
                if (timeSinceLoad < 5.0)
                {
                    if (DateTime.Now.Second % 5 == 0)
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Sign-in screen detected, waiting for UI to settle ({timeSinceLoad:F1}s)...");
                    return;
                }

                // Collect all sign-in button candidates with their screen positions
                var candidates = new List<Button>();
                foreach (Button btn in Resources.FindObjectsOfTypeAll<Button>())
                {
                    if (btn == null || !btn.gameObject.activeInHierarchy || !btn.interactable) continue;
                    string name = btn.name ?? "";
                    string txt = GetButtonText(btn);
                    // Match by known button name first (most reliable)
                    bool isSignInByName =
                        name.Equals("buttonSignInCredentials", StringComparison.OrdinalIgnoreCase) ||
                        name.Equals("btnSignInCredentials", StringComparison.OrdinalIgnoreCase) ||
                        name.IndexOf("SignInCredentials", StringComparison.OrdinalIgnoreCase) >= 0;
                    // Fallback: match by button text
                    bool isSignInByText = !string.IsNullOrEmpty(txt) && (
                        txt.IndexOf("sign in", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        txt.IndexOf("log in", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        txt.IndexOf("login", StringComparison.OrdinalIgnoreCase) >= 0);
                    bool isSkip = txt.IndexOf("skip", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                  txt.IndexOf("guest", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                  txt.IndexOf("anonymous", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                  txt.IndexOf("without", StringComparison.OrdinalIgnoreCase) >= 0;
                    if ((isSignInByName || isSignInByText) && !isSkip)
                        candidates.Add(btn);
                }

                // Log all candidates with positions so we can verify we pick the right one
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found {candidates.Count} sign-in button candidate(s). Screen size: {Screen.width}x{Screen.height}");
                foreach (Button c in candidates)
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin]   Candidate: name='{c.name}' text='{GetButtonText(c)}' screenPos={c.transform.position}");

                if (candidates.Count == 0)
                {
                    // sign-in was detected via text scan but now no candidates — UI might be transitioning
                    return;
                }

                // Pick the button closest to vertical CENTER of screen (not the top nav bar button)
                float centerY = Screen.height / 2.0f;
                Button bestBtn = candidates[0];
                float bestDist = Mathf.Abs(candidates[0].transform.position.y - centerY);
                foreach (Button c in candidates)
                {
                    float dist = Mathf.Abs(c.transform.position.y - centerY);
                    if (dist < bestDist)
                    {
                        bestDist = dist;
                        bestBtn = c;
                    }
                }

                // Exactly one click per sign-in-screen appearance (reduce-login-retry-attempts) —
                // retrying every 30s just delayed noticing a failed attempt, since the give-up
                // threshold below is now shorter than a second click's cooldown would allow anyway.
                if (!signInClickAttempted)
                {
                    signInClickAttempted = true;
                    lastSignInClickTime = DateTime.Now;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking center sign-in button (single attempt): name='{bestBtn.name}' text='{GetButtonText(bestBtn)}' pos={bestBtn.transform.position}");
                    bestBtn.onClick.Invoke();
                }
                else if (DateTime.Now.Second % 5 == 0)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for sign-in response after single attempt ({(DateTime.Now - lastSignInClickTime).TotalSeconds:F0}s / 35s)...");
                }

                // After 35s with no progress (just past the server-side ~30s auth window), go back
                // to MainMenu to reset state rather than waiting out the old 60s cap.
                if (timeSinceLoad > 35.0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Still on sign-in screen after 35s (single attempt exhausted) — returning to MainMenu.");
                    liftoffProLoginAttempted = false;
                    liftoffProLoginClickTime = DateTime.MinValue;
                    signInClickAttempted = false;
                    NavigateToMainMenu();
                }
                return;
            }
            signInWasVisible = false;

            // 3. Check if GameRoom is active
            GameObject gameRoomObj = GameObject.Find("GameRoom");
            bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy);

            if (inRoom)
            {
                lastInRoomTime = DateTime.Now;
                HandleGameRoom();
                return;
            }

            // Not in room — check grace period before doing anything.
            // GameRoom can temporarily disappear during settings updates and Photon state syncs.
            // If we were in a room within the last 120s, hold position — do NOT create a new lobby.
            // Skipped entirely during a /private <name> rename: we left the room on purpose, so
            // there's nothing transient to wait out — go straight to the join-by-name/create logic
            // below. Without this, the grace period silently ate up to 120s doing nothing, and then
            // the stuck-in-menu fallback below could fire immediately afterwards (since sceneLoadTime
            // predates the leave), bouncing the bot out to MainMenu before it ever tried to recover.
            double timeInMenu = (DateTime.Now - sceneLoadTime).TotalSeconds;
            double timeSinceRoom = lastInRoomTime != DateTime.MinValue
                ? (DateTime.Now - lastInRoomTime).TotalSeconds
                : timeInMenu;

            if (!pendingPrivateRoomRename)
            {
                if (lastInRoomTime != DateTime.MinValue && timeSinceRoom < 120.0)
                {
                    if (DateTime.Now.Second % 10 == 0)
                    {
                        bool photonConnected = GetPhotonBoolProperty("IsConnected");
                        bool photonInRoom = GetPhotonBoolProperty("InRoom");
                        bool photonReady = GetPhotonBoolProperty("IsConnectedAndReady");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] GameRoom not found but was in room {timeSinceRoom:F0}s ago (grace period 120s). Photon Status: IsConnected={photonConnected}, InRoom={photonInRoom}, IsConnectedAndReady={photonReady}");
                    }
                    return;
                }

                // Stuck-in-menu fallback: only fires after grace period
                if (timeInMenu > 90.0 && timeSinceRoom > 120.0)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Stuck in MultiplayerMenu for {timeInMenu:F0}s (out of room for {timeSinceRoom:F0}s) — navigating back to MainMenu.");
                    NavigateToMainMenu();
                    return;
                }
            }

            // Grace period expired (or never been in a room this scene load) — reset state
            roomCreatedTime = DateTime.MinValue;
            isLeaving = false;

            // 3b. If a /private <name> request hit a name collision, drive the join-by-name UI instead of Create Game
            if (pendingJoinByName && !joinByNamePanelSubmitted)
            {
                ProcessJoinByNameFlow();
                return;
            }

            // 4. Lobby (List of games): If we are on the Lobby screen, click Create Game
            string[] createNames = { "buttonCreateGame", "btnCreateGame", "CreateGame" };
            Button createBtn = FindButtonByTextOrName("CREATE GAME", createNames);
            if (createBtn == null)
            {
                // Try text containing "create game" or "create"
                Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
                foreach (var b in buttons)
                {
                    if (b == null) continue;
                    string txt = GetButtonText(b);
                    if (txt.IndexOf("create game", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        createBtn = b;
                        break;
                    }
                }
            }

            if (createBtn != null && createBtn.gameObject.activeInHierarchy)
            {
                bool isReady = IsMultiplayerClientReady();
                if (DateTime.Now.Second % 5 == 0)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Create button found. Interactable: {createBtn.interactable}, ClientReady: {isReady}");
                }
                if (createBtn.interactable && isReady)
                {
                    try
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking Create Game button: {createBtn.name}");
                        createBtn.onClick.Invoke();
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Clicking Create Game button failed: {ex.Message}");
                    }
                }
                return;
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
                roomCreatedTime = DateTime.Now;
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

            double elapsed = (DateTime.Now - roomCreatedTime).TotalSeconds;

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
                    string[] cycleColors = { "#FF0000", "#00FFFF", "#FF00FF" };
                    string cycleColor = cycleColors[(trackIdx % cycleColors.Length + cycleColors.Length) % cycleColors.Length];
                    SendChatMessage($"<b><color=#FF0000>[SYSTEM]</color></b> Up next: <b><color={cycleColor}>{nextEnv} - {nextTrackName}</color></b>");
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
                    
                    // Reset room timer if we are in a room
                    GameObject gameRoomObj = GameObject.Find("GameRoom");
                    bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy);
                    if (inRoom)
                    {
                        roomCreatedTime = DateTime.Now;
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
                        roomCreatedTime = DateTime.Now; // Reset the rotation timer!
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
                    }
                }
            }
        }

        private static Type FindType(string fullName)
        {
            try
            {
                foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    var type = assembly.GetType(fullName);
                    if (type != null) return type;
                }
            }
            catch {}
            return null;
        }

        private const int CHAT_MAX_CHARS = 220;

        private static string ParseTag(string s, int index, out int nextIndex)
        {
            nextIndex = index;
            if (index >= s.Length || s[index] != '<') return null;

            int end = s.IndexOf('>', index);
            if (end == -1) return null;

            nextIndex = end + 1;
            return s.Substring(index, end - index + 1);
        }

        private static void CloseLastTag(List<string> openTags, string closingTag)
        {
            string target = "";
            if (closingTag == "</b>") target = "<b>";
            else if (closingTag == "</i>") target = "<i>";
            else if (closingTag == "</color>") target = "<color";

            if (string.IsNullOrEmpty(target)) return;

            for (int i = openTags.Count - 1; i >= 0; i--)
            {
                if (target == "<color" ? openTags[i].StartsWith("<color", StringComparison.OrdinalIgnoreCase) : openTags[i].Equals(target, StringComparison.OrdinalIgnoreCase))
                {
                    openTags.RemoveAt(i);
                    break;
                }
            }
        }

        private static int GetClosingTagsLength(List<string> openTags)
        {
            if (openTags == null) return 0;
            int len = 0;
            foreach (var tag in openTags)
            {
                if (tag.StartsWith("<color", StringComparison.OrdinalIgnoreCase)) len += 8; // </color>
                else if (tag.Equals("<b>", StringComparison.OrdinalIgnoreCase)) len += 4; // </b>
                else if (tag.Equals("<i>", StringComparison.OrdinalIgnoreCase)) len += 4; // </i>
            }
            return len;
        }

        private static string GetClosingTagsString(List<string> openTags)
        {
            if (openTags == null) return "";
            StringBuilder sb = new StringBuilder();
            for (int i = openTags.Count - 1; i >= 0; i--)
            {
                string tag = openTags[i];
                if (tag.StartsWith("<color", StringComparison.OrdinalIgnoreCase)) sb.Append("</color>");
                else if (tag.Equals("<b>", StringComparison.OrdinalIgnoreCase)) sb.Append("</b>");
                else if (tag.Equals("<i>", StringComparison.OrdinalIgnoreCase)) sb.Append("</i>");
            }
            return sb.ToString();
        }

        private static string GetOpeningTagsString(List<string> openTags)
        {
            if (openTags == null) return "";
            StringBuilder sb = new StringBuilder();
            foreach (var tag in openTags)
            {
                sb.Append(tag);
            }
            return sb.ToString();
        }

        private static List<string> SplitMessage(string message, int maxChars)
        {
            List<string> result = new List<string>();
            string currentString = message;

            while (currentString.Length > maxChars)
            {
                int n = currentString.Length;
                List<string>[] tagsAt = new List<string>[n];
                bool[] inTag = new bool[n];

                List<string> activeTags = new List<string>();
                int idx = 0;
                while (idx < n)
                {
                    if (currentString[idx] == '<')
                    {
                        int nextIdx;
                        string tag = ParseTag(currentString, idx, out nextIdx);
                        if (tag != null)
                        {
                            bool isClosing = tag.StartsWith("</");
                            for (int j = idx; j < nextIdx; j++)
                            {
                                inTag[j] = true;
                                tagsAt[j] = new List<string>(activeTags);
                            }
                            if (isClosing)
                            {
                                CloseLastTag(activeTags, tag.ToLower());
                            }
                            else
                            {
                                activeTags.Add(tag);
                            }
                            idx = nextIdx;
                            continue;
                        }
                    }
                    tagsAt[idx] = new List<string>(activeTags);
                    inTag[idx] = false;
                    idx++;
                }

                int bestSplitIdx = -1;
                int searchEnd = maxChars;

                // 1. Search for " | " separator
                int pipesIndex = -1;
                for (int i = searchEnd - 3; i >= 0; i--)
                {
                    if (i + 3 <= n && currentString.Substring(i, 3) == " | " && !inTag[i])
                    {
                        int candidateSplit = i + 3;
                        List<string> openTags = (candidateSplit >= 0 && candidateSplit < tagsAt.Length) ? tagsAt[candidateSplit] : new List<string>();
                        int closingLen = GetClosingTagsLength(openTags);
                        if (candidateSplit + closingLen <= maxChars)
                        {
                            pipesIndex = candidateSplit;
                            break;
                        }
                    }
                }

                if (pipesIndex != -1)
                {
                    bestSplitIdx = pipesIndex;
                }
                else
                {
                    // 2. Search for space character
                    int spaceIndex = -1;
                    for (int i = searchEnd - 1; i >= 0; i--)
                    {
                        if (currentString[i] == ' ' && !inTag[i])
                        {
                            int candidateSplit = i + 1;
                            List<string> openTags = (candidateSplit >= 0 && candidateSplit < tagsAt.Length) ? tagsAt[candidateSplit] : new List<string>();
                            int closingLen = GetClosingTagsLength(openTags);
                            if (candidateSplit + closingLen <= maxChars)
                            {
                                spaceIndex = candidateSplit;
                                break;
                            }
                        }
                    }

                    if (spaceIndex != -1)
                    {
                        bestSplitIdx = spaceIndex;
                    }
                    else
                    {
                        // 3. Absolute fallback: split at character boundary
                        for (int i = searchEnd; i >= 1; i--)
                        {
                            if (!inTag[i - 1])
                            {
                                List<string> openTags = (i >= 0 && i < tagsAt.Length) ? tagsAt[i] : new List<string>();
                                int closingLen = GetClosingTagsLength(openTags);
                                if (i + closingLen <= maxChars)
                                {
                                    bestSplitIdx = i;
                                    break;
                                }
                            }
                        }
                    }
                }

                if (bestSplitIdx <= 0)
                {
                    bestSplitIdx = maxChars;
                }

                if (bestSplitIdx >= n)
                {
                    break;
                }

                string chunk = currentString.Substring(0, bestSplitIdx);
                List<string> openTagsAtSplit = (bestSplitIdx >= 0 && bestSplitIdx < tagsAt.Length) ? tagsAt[bestSplitIdx] : new List<string>();
                string closingTags = GetClosingTagsString(openTagsAtSplit);
                chunk += closingTags;
                result.Add(chunk);

                string openingTags = GetOpeningTagsString(openTagsAtSplit);
                currentString = openingTags + currentString.Substring(bestSplitIdx);
            }

            if (!string.IsNullOrEmpty(currentString))
            {
                result.Add(currentString);
            }
            return result;
        }

        private static void SendChatMessage(string message)
        {
            if (string.IsNullOrEmpty(message)) return;

            if (message.Length <= CHAT_MAX_CHARS)
            {
                SendChatMessageRaw(message);
                return;
            }

            try
            {
                List<string> chunks = SplitMessage(message, CHAT_MAX_CHARS);
                foreach (string chunk in chunks)
                {
                    SendChatMessageRaw(chunk);
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in SendChatMessage splitting: {ex}");
                // Fallback to sending raw if splitting fails for some reason
                SendChatMessageRaw(message);
            }
        }

        private static void SendChatMessageRaw(string message)
        {
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] SendChatMessage called: '{message}'");
            try
            {
                Type chatType = FindType("Liftoff.Multiplayer.Chat.ChatWindowPanel");
                if (chatType != null)
                {
                    var chats = Resources.FindObjectsOfTypeAll(chatType);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found {chats.Length} ChatWindowPanel objects.");
                    // Prefer an active panel — during a room recreate, a stale inactive instance
                    // from the old scene can briefly coexist with the new one.
                    object chatObj = chats.FirstOrDefault(c => c != null && ((MonoBehaviour)c).gameObject.activeInHierarchy) ?? chats.FirstOrDefault(c => c != null);
                    if (chatObj != null)
                    {
                        MonoBehaviour chatPanel = (MonoBehaviour)chatObj;
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] chatPanel activeInHierarchy: {chatPanel.gameObject.activeInHierarchy}");
                        var inputFieldField = chatType.GetField("fieldUserMessage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                        if (inputFieldField != null)
                        {
                            UnityEngine.UI.InputField inputField = (UnityEngine.UI.InputField)inputFieldField.GetValue(chatPanel);
                            if (inputField != null)
                            {
                                inputField.text = message;
                                var sendMethod = chatType.GetMethod("SendUserMessage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                                if (sendMethod != null)
                                {
                                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Invoking SendUserMessage on ChatWindowPanel.");
                                    sendMethod.Invoke(chatPanel, null);
                                }
                                else
                                {
                                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] SendUserMessage method not found.");
                                }
                            }
                            else
                            {
                                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] fieldUserMessage is null.");
                            }
                        }
                        else
                        {
                            UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] fieldUserMessage field not found.");
                        }
                    }
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] ChatWindowPanel type not found.");
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to send chat message: {ex.Message}");
            }
        }

        private static string PeekNextTrackName(out string environment, out string gameMode, out int trackIndex)
        {
            environment = "The Drawing Board";
            gameMode = "Classic Race";
            trackIndex = 0;
            UnityEngine.Debug.Log("[AutoLobbyPlugin] PeekNextTrackName called.");
            try
            {
                string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] tracksPath: {tracksPath}, statePath: {statePath}");

                if (!File.Exists(tracksPath))
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracksPath does not exist.");
                    return "";
                }

                string[] lines = File.ReadAllLines(tracksPath);
                var validTracks = new List<string>();
                foreach (var line in lines)
                {
                    if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#"))
                    {
                        validTracks.Add(line.Trim());
                    }
                }

                if (validTracks.Count == 0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] No valid tracks parsed.");
                    return "";
                }

                // Sync shuffleMode from disk
                shuffleMode = GetShuffleMode();

                int index = 0;
                List<int> shuffledIndices = null;
                int shuffleIndex = 0;
                if (shuffleMode)
                {
                    string shuffleStatePath = Path.Combine(pluginPath, "shuffle_state.txt");
                    if (!ParseShuffleState(shuffleStatePath, validTracks.Count, out shuffleIndex, out shuffledIndices))
                    {
                        shuffledIndices = null;
                    }
                    else if (shuffleIndex < 0 || shuffleIndex >= shuffledIndices.Count)
                    {
                        shuffleIndex = 0;
                    }
                }
                else
                {
                    if (File.Exists(statePath))
                    {
                        int.TryParse(File.ReadAllText(statePath).Trim(), out index);
                    }
                    if (index < 0 || index >= validTracks.Count)
                    {
                        index = 0;
                    }
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Current state index: {index}, validTracks count: {validTracks.Count}");

                string overrideMode = GetOverrideGameMode();
                string trackName = "";

                // Walk forward from the current position (read-only, no state is persisted by
                // Peek) skipping any session-blacklisted (env, track, mode) combos, so the "up
                // next" chat announcement never names a track that will be skipped instantly.
                for (int attempt = 0; attempt < validTracks.Count; attempt++)
                {
                    int candidateIndex = (shuffleMode && shuffledIndices != null)
                        ? shuffledIndices[(shuffleIndex + attempt) % shuffledIndices.Count]
                        : (index + attempt) % validTracks.Count;

                    string selectedLine = validTracks[candidateIndex];
                    string[] parts = selectedLine.Split(',');
                    string candidateTrackName = parts[0].Trim();
                    string candidateEnv = parts.Length > 1 ? parts[1].Trim() : environment;
                    string candidateMode = parts.Length > 2 ? parts[2].Trim() : gameMode;
                    if (!string.IsNullOrEmpty(overrideMode)) candidateMode = overrideMode;

                    string key = $"{candidateEnv}|{candidateTrackName}|{candidateMode}";
                    if (sessionBlacklistedTracks.Contains(key)) continue;

                    trackIndex = candidateIndex;
                    environment = candidateEnv;
                    gameMode = candidateMode;
                    trackName = candidateTrackName;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Selected line: '{selectedLine}'");
                    break;
                }

                return trackName;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PeekNextTrackName: {ex.Message}");
                return "";
            }
        }

        private static int CountValidRotationLines()
        {
            try
            {
                string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                if (!File.Exists(tracksPath)) return 0;
                int count = 0;
                foreach (var line in File.ReadAllLines(tracksPath))
                {
                    if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#")) count++;
                }
                return count;
            }
            catch
            {
                return 0;
            }
        }

        // Wraps GetNextTrackFromRotationOnce() with a bounded skip-loop so tracks that failed at
        // runtime this session (see sessionBlacklistedTracks) are never selected again until the
        // process restarts. Bounded by the rotation's own line count so a fully-blacklisted
        // rotation can't loop forever.
        private static string GetNextTrackFromRotation(out string environment, out string gameMode)
        {
            string trackName = "";
            environment = "The Drawing Board";
            gameMode = "Classic Race";

            int maxAttempts = Math.Max(1, CountValidRotationLines());
            for (int attempt = 0; attempt < maxAttempts; attempt++)
            {
                trackName = GetNextTrackFromRotationOnce(out environment, out gameMode);
                if (string.IsNullOrEmpty(trackName)) break;

                string key = $"{environment}|{trackName}|{gameMode}";
                if (!sessionBlacklistedTracks.Contains(key)) break;

                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Skipping session-blacklisted track '{trackName}' ({environment}, {gameMode}) in rotation.");
            }

            return trackName;
        }

        private static string GetNextTrackFromRotationOnce(out string environment, out string gameMode)
        {
            environment = "The Drawing Board";
            gameMode = "Classic Race";

            try
            {
                string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                string statePath = Path.Combine(pluginPath, "rotation_state.txt");

                if (!File.Exists(tracksPath))
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracks_to_rotate.txt not found. Using default values.");
                    return "";
                }

                string[] lines = File.ReadAllLines(tracksPath);
                var validTracks = new List<string>();
                foreach (var line in lines)
                {
                    if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#"))
                    {
                        validTracks.Add(line.Trim());
                    }
                }

                if (validTracks.Count == 0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracks_to_rotate.txt is empty.");
                    return "";
                }

                // Sync shuffleMode from disk
                shuffleMode = GetShuffleMode();

                int index = 0;
                if (shuffleMode)
                {
                    string shuffleStatePath = Path.Combine(pluginPath, "shuffle_state.txt");
                    int shuffleIndex;
                    List<int> shuffledIndices;
                    
                    if (!ParseShuffleState(shuffleStatePath, validTracks.Count, out shuffleIndex, out shuffledIndices))
                    {
                        shuffledIndices = GenerateShuffledIndices(validTracks.Count);
                        shuffleIndex = 0;
                    }
                    
                    if (shuffleIndex < 0 || shuffleIndex >= shuffledIndices.Count)
                    {
                        shuffleIndex = 0;
                    }
                    
                    index = shuffledIndices[shuffleIndex];
                    
                    int nextShuffleIndex = shuffleIndex + 1;
                    if (nextShuffleIndex >= shuffledIndices.Count)
                    {
                        // Generate a new shuffle for the next cycle
                        var nextShuffled = GenerateShuffledIndices(validTracks.Count);
                        SaveShuffleState(shuffleStatePath, 0, nextShuffled);
                    }
                    else
                    {
                        SaveShuffleState(shuffleStatePath, nextShuffleIndex, shuffledIndices);
                    }
                }
                else
                {
                    if (File.Exists(statePath))
                        int.TryParse(File.ReadAllText(statePath).Trim(), out index);

                    if (index < 0 || index >= validTracks.Count)
                        index = 0;

                    int nextIndex = (index + 1) % validTracks.Count;
                    File.WriteAllText(statePath, nextIndex.ToString());
                }

                string selectedLine = validTracks[index];

                // Parse line: TrackName,EnvironmentName,GameModeName
                string[] parts = selectedLine.Split(',');
                string trackName = parts[0].Trim();
                if (parts.Length > 1) environment = parts[1].Trim();
                if (parts.Length > 2) gameMode = parts[2].Trim();

                string overrideMode = GetOverrideGameMode();
                if (!string.IsNullOrEmpty(overrideMode))
                {
                    gameMode = overrideMode;
                }

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Selection: '{trackName}' (Env: '{environment}', Mode: '{gameMode}') [Index {index}]");
                return trackName;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in GetNextTrackFromRotation: {ex.Message}");
                return "";
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

        private static double GetRotationInterval()
        {
            try
            {
                string intervalPath = Path.Combine(pluginPath, "rotation_interval.txt");
                if (File.Exists(intervalPath))
                {
                    double val;
                    if (double.TryParse(File.ReadAllText(intervalPath).Trim(), out val))
                    {
                        return val;
                    }
                }
            }
            catch {}
            return 600.0; // Default: 10 mins
        }

        private static bool GetAutoStart()
        {
            try
            {
                string autoStartPath = Path.Combine(pluginPath, "auto_start.txt");
                if (File.Exists(autoStartPath))
                {
                    string content = File.ReadAllText(autoStartPath).Trim();
                    return content.Equals("true", StringComparison.OrdinalIgnoreCase);
                }
            }
            catch {}
            return false; // Default: stay in lobby
        }

        private static bool GetShuffleMode()
        {
            try
            {
                string shuffleModePath = Path.Combine(pluginPath, "shuffle_mode.txt");
                if (File.Exists(shuffleModePath))
                {
                    string content = File.ReadAllText(shuffleModePath).Trim();
                    return content.Equals("true", StringComparison.OrdinalIgnoreCase);
                }
            }
            catch {}
            return false; // Default: false
        }

        private static string GetOverrideGameMode()
        {
            try
            {
                string path = Path.Combine(pluginPath, "override_game_mode.txt");
                if (File.Exists(path))
                {
                    string mode = File.ReadAllText(path).Trim();
                    if (!string.IsNullOrEmpty(mode))
                    {
                        return mode;
                    }
                }
            }
            catch {}
            return null;
        }

        private static bool PlaylistExists(string name)
        {
            try
            {
                string path = Path.Combine(pluginPath, "available_playlists.txt");
                if (File.Exists(path))
                {
                    foreach (var line in File.ReadAllLines(path))
                    {
                        if (line.Trim().Equals(name.Trim(), StringComparison.OrdinalIgnoreCase))
                        {
                            return true;
                        }
                    }
                }
            }
            catch {}
            return false;
        }

        private static string GetAvailablePlaylistsString()
        {
            try
            {
                string path = Path.Combine(pluginPath, "available_playlists.txt");
                if (File.Exists(path))
                {
                    var list = new List<string>();
                    foreach (var line in File.ReadAllLines(path))
                    {
                        if (!string.IsNullOrWhiteSpace(line))
                        {
                            list.Add(line.Trim());
                        }
                    }
                    return string.Join(", ", list.ToArray());
                }
            }
            catch {}
            return "";
        }

        private static bool KickPlayer(string targetName, out string matchedName, out string matchesList)
        {
            matchedName = "";
            matchesList = "";
            try
            {
                Type networkType = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                                   Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (networkType == null) return false;

                PropertyInfo playerListProp = networkType.GetProperty("PlayerList", BindingFlags.Public | BindingFlags.Static);
                if (playerListProp == null) return false;

                Array playerArray = (Array)playerListProp.GetValue(null);
                if (playerArray == null || playerArray.Length == 0) return false;

                var matches = new List<object>();
                var matchNames = new List<string>();

                for (int i = 0; i < playerArray.Length; i++)
                {
                    object playerObj = playerArray.GetValue(i);
                    if (playerObj == null) continue;

                    PropertyInfo nickProp = playerObj.GetType().GetProperty("NickName") ?? playerObj.GetType().GetProperty("Nickname");
                    if (nickProp == null) continue;

                    string nick = (string)nickProp.GetValue(playerObj, null) ?? "";
                    
                    if (nick.IndexOf(targetName, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        PropertyInfo localProp = playerObj.GetType().GetProperty("IsLocal");
                        bool isLocal = false;
                        if (localProp != null) isLocal = (bool)localProp.GetValue(playerObj, null);
                        if (isLocal) continue;

                        matches.Add(playerObj);
                        matchNames.Add(nick);
                    }
                }

                if (matches.Count == 0)
                {
                    return false;
                }
                if (matches.Count == 1)
                {
                    matchedName = matchNames[0];
                    MethodInfo closeMethod = networkType.GetMethod("CloseConnection", BindingFlags.Public | BindingFlags.Static, null, new[] { matches[0].GetType() }, null);
                    if (closeMethod != null)
                    {
                        closeMethod.Invoke(null, new[] { matches[0] });
                        return true;
                    }
                }
                else
                {
                    matchedName = "multiple";
                    matchesList = string.Join(", ", matchNames.ToArray());
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception in KickPlayer: {ex}");
            }
            return false;
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

        private static bool ParseShuffleState(string shuffleStatePath, int validTracksCount, out int shuffleIndex, out List<int> shuffledIndices)
        {
            shuffleIndex = 0;
            shuffledIndices = new List<int>();
            try
            {
                if (File.Exists(shuffleStatePath))
                {
                    string content = File.ReadAllText(shuffleStatePath).Trim();
                    string[] parts = content.Split('|');
                    if (parts.Length == 2)
                    {
                        if (int.TryParse(parts[0], out shuffleIndex))
                        {
                            string[] indexStrings = parts[1].Split(',');
                            foreach (var s in indexStrings)
                            {
                                int idx;
                                if (int.TryParse(s.Trim(), out idx))
                                {
                                    shuffledIndices.Add(idx);
                                }
                            }
                            
                            if (shuffledIndices.Count == validTracksCount)
                            {
                                var sortedIndices = new List<int>(shuffledIndices);
                                sortedIndices.Sort();
                                for (int i = 0; i < validTracksCount; i++)
                                {
                                    if (sortedIndices[i] != i)
                                    {
                                        return false;
                                    }
                                }
                                return true;
                            }
                        }
                    }
                }
            }
            catch {}
            return false;
        }

        private static List<int> GenerateShuffledIndices(int count)
        {
            List<int> list = new List<int>();
            for (int i = 0; i < count; i++)
            {
                list.Add(i);
            }
            for (int i = list.Count - 1; i > 0; i--)
            {
                int j = rng.Next(0, i + 1);
                int temp = list[i];
                list[i] = list[j];
                list[j] = temp;
            }
            return list;
        }

        private static void SaveShuffleState(string shuffleStatePath, int shuffleIndex, List<int> shuffledIndices)
        {
            try
            {
                System.Text.StringBuilder sb = new System.Text.StringBuilder();
                sb.Append(shuffleIndex);
                sb.Append("|");
                for (int i = 0; i < shuffledIndices.Count; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append(shuffledIndices[i]);
                }
                File.WriteAllText(shuffleStatePath, sb.ToString());
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error saving shuffle state: {ex.Message}");
            }
        }


        private static void ApplyHarmonyPatches()
        {
            try
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Applying Harmony patches...");
                
                Assembly asm = Assembly.Load("Assembly-CSharp");
                Type shareableType = asm.GetType("ShareableContent") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "ShareableContent");
                if (shareableType == null)
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] Could not find ShareableContent type.");
                    return;
                }

                Type targetType = null;
                MethodInfo targetMethod = null;

                foreach (Type t in asm.GetTypes())
                {
                    if (t.BaseType != typeof(object)) continue;
                    
                    var fields = t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    bool hasListField = fields.Any(f => f.FieldType.IsGenericType && 
                                                        f.FieldType.GetGenericTypeDefinition() == typeof(List<>) && 
                                                        f.FieldType.GetGenericArguments()[0] == shareableType);
                    if (!hasListField) continue;

                    bool hasContentTypeField = fields.Any(f => f.FieldType.Name == "ContentType");
                    if (!hasContentTypeField) continue;

                    var methods = t.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    foreach (var m in methods)
                    {
                        if (m.ReturnType == typeof(bool))
                        {
                            var pars = m.GetParameters();
                            if (pars.Length == 2 && pars[0].ParameterType == shareableType && pars[1].ParameterType == typeof(bool))
                            {
                                targetType = t;
                                targetMethod = m;
                                break;
                            }
                        }
                    }
                    if (targetType != null) break;
                }

                if (targetMethod != null)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found validator method to patch: {targetType.FullName}::{targetMethod.Name}");
                    
                    var harmony = new HarmonyLib.Harmony("com.lugus.liftoff.autolobby.patch");
                    var prefixMethod = typeof(AutoLobbyPlugin).GetMethod("ValidationPrefix", BindingFlags.NonPublic | BindingFlags.Static);
                    
                    if (prefixMethod != null)
                    {
                        harmony.Patch(targetMethod, prefix: new HarmonyLib.HarmonyMethod(prefixMethod));
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Harmony patch applied successfully!");
                    }
                    else
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] ValidationPrefix method not found in plugin.");
                    }
                }
                else
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] Target validator method not found in Assembly-CSharp.");
                }

                // Patch ChatMessagePatch
                try
                {
                    var chatTarget = ChatMessagePatch.TargetMethod();
                    if (chatTarget != null)
                    {
                        var chatPostfix = typeof(ChatMessagePatch).GetMethod("Postfix", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                        if (chatPostfix != null)
                        {
                            var harmony = new HarmonyLib.Harmony("com.lugus.liftoff.autolobby.chat");
                            harmony.Patch(chatTarget, postfix: new HarmonyLib.HarmonyMethod(chatPostfix));
                            UnityEngine.Debug.Log("[AutoLobbyPlugin] ChatMessagePatch applied successfully!");
                        }
                        else
                        {
                            UnityEngine.Debug.LogError("[AutoLobbyPlugin] ChatMessagePatch Postfix method not found.");
                        }
                    }
                    else
                    {
                        UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] ChatWindowPanel.GenerateUserMessage target method not found.");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to patch ChatMessagePatch: {ex}");
                }

                // Patch RaceLinesVisualizer.CreateInstance to suppress null instantiation exceptions
                try
                {
                    Type visualizerType = asm.GetType("RaceLinesVisualizer") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "RaceLinesVisualizer");
                    if (visualizerType != null)
                    {
                        MethodInfo createInstanceMethod = visualizerType.GetMethod("CreateInstance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static);
                        if (createInstanceMethod != null)
                        {
                            UnityEngine.Debug.Log("[AutoLobbyPlugin] Found RaceLinesVisualizer.CreateInstance! Patching with finalizer.");
                            var harmony = new HarmonyLib.Harmony("com.lugus.liftoff.autolobby.visualizer");
                            var finalizerMethod = typeof(AutoLobbyPlugin).GetMethod("CreateInstanceFinalizer", BindingFlags.NonPublic | BindingFlags.Static);
                            if (finalizerMethod != null)
                            {
                                harmony.Patch(createInstanceMethod, finalizer: new HarmonyLib.HarmonyMethod(finalizerMethod));
                                UnityEngine.Debug.Log("[AutoLobbyPlugin] RaceLinesVisualizer.CreateInstance patch applied successfully!");
                            }
                            else
                            {
                                UnityEngine.Debug.LogError("[AutoLobbyPlugin] CreateInstanceFinalizer method not found.");
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Visualizer patch failed: {ex.Message}");
                }

                // Patch Photon in-room callbacks to prevent visualizer/sync exceptions from crashing the room synchronization
                try
                {
                    Assembly photonRealtimeAsm = Assembly.Load("PhotonRealtime");
                    string[] callbackContainerTypes = photonRealtimeAsm.GetTypes()
                        .Where(t => t.Name.EndsWith("CallbacksContainer") || t.Name.Contains("CallbackContainer"))
                        .Select(t => t.FullName)
                        .ToArray();

                    var harmony = new HarmonyLib.Harmony("com.lugus.liftoff.autolobby.photon");
                    var prefixMethod = typeof(AutoLobbyPlugin).GetMethod("PhotonContainerPrefix", BindingFlags.NonPublic | BindingFlags.Static);
                    
                    if (prefixMethod != null)
                    {
                        foreach (string typeName in callbackContainerTypes)
                        {
                            Type containerType = photonRealtimeAsm.GetType(typeName);
                            if (containerType != null)
                            {
                                var methods = containerType.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                                int patchedCount = 0;
                                foreach (var method in methods)
                                {
                                    if (method.Name.StartsWith("On"))
                                    {
                                        harmony.Patch(method, prefix: new HarmonyLib.HarmonyMethod(prefixMethod));
                                        patchedCount++;
                                    }
                                }
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Successfully patched {patchedCount} callbacks on {typeName} with try-catch loop prefix.");
                            }
                            else
                            {
                                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Could not find Photon type: {typeName}");
                            }
                        }
                    }
                    else
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] PhotonContainerPrefix method not found.");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Photon callbacks patching failed: {ex.Message}");
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Harmony patching failed: {ex}");
            }
        }

        private static bool ValidationPrefix(object __instance, object[] __args, ref bool __result)
        {
            if (__args == null || __args.Length == 0) return true;
            object item = __args[0];
            if (item == null)
            {
                __result = false;
                return false;
            }

            try
            {
                FieldInfo contentField = __instance.GetType().GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                    .FirstOrDefault(f => f.FieldType.Name == "ContentType");

                if (contentField != null)
                {
                    object depotTypeVal = contentField.GetValue(__instance);
                    PropertyInfo typeProp = item.GetType().GetProperty("Type", BindingFlags.Public | BindingFlags.Instance);
                    if (typeProp != null)
                    {
                        object itemTypeVal = typeProp.GetValue(item);
                        if (depotTypeVal.ToString() != itemTypeVal.ToString())
                        {
                            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Bypassed type mismatch: item '{item}' type '{itemTypeVal}' does not match depot type '{depotTypeVal}'.");
                            __result = false;
                            return false;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in validation prefix: {ex.Message}");
            }

            return true;
        }

        private static Exception CreateInstanceFinalizer(Exception __exception)
        {
            if (__exception != null)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Suppressed exception in RaceLinesVisualizer.CreateInstance: {__exception}");
            }
            return null; // Suppress the exception!
        }

        private static bool PhotonContainerPrefix(object __instance, MethodBase __originalMethod, object[] __args)
        {
            try
            {
                string methodName = __originalMethod.Name;
                if (methodName == "OnLeftRoom" || methodName == "OnDisconnected")
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Photon Callback: {methodName} detected. Immediately resetting lastInRoomTime to trigger lobby recovery.");
                    lastInRoomTime = DateTime.MinValue;
                    roomCreatedTime = DateTime.MinValue;
                    isLeaving = false;
                }
                else if (methodName == "OnCreateRoomFailed" && __args != null && __args.Length >= 2)
                {
                    // Not gated on pendingPrivateRoomRename: any create attempt (bot startup,
                    // post-disconnect recreate, etc.) can hit a stale/occupied room name, not just
                    // an explicit /private <name> request — always try to recover.
                    HandleCreateRoomFailed((short)__args[0], __args[1] as string);
                }
                else if (methodName == "OnJoinRoomFailed" && joinByNamePanelSubmitted && __args != null && __args.Length >= 2)
                {
                    HandleJoinByNameFailed((short)__args[0], __args[1] as string);
                }
                else if (methodName == "OnCreatedRoom")
                {
                    roomOwnedByBot = true;
                    if (pendingPrivateRoomRename)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Private room rename: new room created successfully.");
                        QueueChatMessage($"<color=#0000FF>[ADMIN]</color> Room recreated as private. Join name: <color=#00FF88><i>{pendingPrivateRoomName}</i></color>.");
                    }
                    pendingPrivateRoomRename = false;
                    pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                    pendingJoinByName = false;
                    joinByNamePanelSubmitted = false;
                }
                else if (methodName == "OnJoinedRoom" && joinByNamePanelSubmitted)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Joined an existing room by name instead of creating one — bot does not own this room.");
                    roomOwnedByBot = false;
                    QueueChatMessage($"<color=#0000FF>[ADMIN]</color> A room named '<color=#00FF88><i>{pendingPrivateRoomName}</i></color>' already existed — joined it instead of creating a new one. <color=#FF0000><i>This bot is not the room owner and cannot control settings/rotation here.</i></color> Current host: please transfer host to this bot from the player list so it can control settings/rotation, or use /private with a different name to have the bot create its own room instead.");
                    pendingPrivateRoomRename = false;
                    pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                    pendingJoinByName = false;
                    joinByNamePanelSubmitted = false;
                }
                else if (methodName == "OnMasterClientSwitched" && __args != null && __args.Length >= 1)
                {
                    HandleMasterClientSwitched(__args[0]);
                }

                System.Collections.IList list = __instance as System.Collections.IList;
                if (list == null) return true;

                // Copy targets to avoid collection modified exceptions
                object[] targets;
                lock (list)
                {
                    targets = new object[list.Count];
                    list.CopyTo(targets, 0);
                }

                // Find the interface type that defines this callback
                Type interfaceType = null;
                foreach (var iface in __instance.GetType().GetInterfaces())
                {
                    if (iface.Name.EndsWith("Callbacks") || iface.Name.Contains("Callback"))
                    {
                        interfaceType = iface;
                        break;
                    }
                }

                if (interfaceType == null) return true;

                // Resolve the interface method matching name and parameter types
                var paramTypes = __originalMethod.GetParameters().Select(p => p.ParameterType).ToArray();
                MethodInfo interfaceMethod = interfaceType.GetMethod(__originalMethod.Name, paramTypes);
                if (interfaceMethod == null) return true;

                foreach (var callback in targets)
                {
                    if (callback == null) continue;
                    try
                    {
                        interfaceMethod.Invoke(callback, __args);
                    }
                    catch (Exception ex)
                    {
                        // Log the actual underlying exception
                        Exception realEx = ex.InnerException ?? ex;
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Suppressed exception in {interfaceType.Name} listener ({callback.GetType().FullName}): {realEx}");
                    }
                }

                return false; // Skip the original looping method which would abort on exception
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PhotonContainerPrefix: {ex}");
                return true; // Fallback to original method on error
            }
        }

        // ---------------------------------------------------------------
        // Room visibility / max players / private-room-rename (admin commands)
        // ---------------------------------------------------------------

        private static Type GetPhotonNetworkType()
        {
            return Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                   Type.GetType("PhotonNetwork, Assembly-CSharp");
        }

        private static object GetPhotonCurrentRoom()
        {
            try
            {
                Type type = GetPhotonNetworkType();
                PropertyInfo prop = type?.GetProperty("CurrentRoom", BindingFlags.Public | BindingFlags.Static);
                return prop?.GetValue(null);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to read PhotonNetwork.CurrentRoom: {ex.Message}");
                return null;
            }
        }

        // Sets IsVisible only — a private room stays IsOpen so it can still be joined by name.
        private static bool SetRoomVisibility(bool makePrivate, out string roomName, out string error)
        {
            roomName = "";
            error = "";
            object room = GetPhotonCurrentRoom();
            if (room == null) { error = "not currently in a room"; return false; }
            try
            {
                Type roomType = room.GetType();
                PropertyInfo visibleProp = roomType.GetProperty("IsVisible", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                PropertyInfo nameProp = roomType.GetProperty("Name", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                if (visibleProp == null) { error = "IsVisible property not found"; return false; }
                visibleProp.SetValue(room, !makePrivate);
                roomName = nameProp?.GetValue(room) as string ?? "";
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to set room visibility: {ex}");
                return false;
            }
        }

        private static bool TryGetRoomInfo(out bool isVisible, out string roomName, out int maxPlayers, out int playerCount)
        {
            isVisible = true; roomName = ""; maxPlayers = 0; playerCount = 0;
            object room = GetPhotonCurrentRoom();
            if (room == null) return false;
            try
            {
                Type roomType = room.GetType();
                const BindingFlags roomPropFlags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
                isVisible = (bool)(roomType.GetProperty("IsVisible", roomPropFlags)?.GetValue(room) ?? true);
                roomName = roomType.GetProperty("Name", roomPropFlags)?.GetValue(room) as string ?? "";
                maxPlayers = (byte)(roomType.GetProperty("MaxPlayers", roomPropFlags)?.GetValue(room) ?? (byte)0);
                playerCount = (byte)(roomType.GetProperty("PlayerCount", roomPropFlags)?.GetValue(room) ?? (byte)0);
                return true;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to read room info: {ex.Message}");
                return false;
            }
        }

        private static bool SetRoomMaxPlayers(int requested, out int applied, out string error)
        {
            applied = requested;
            error = "";
            object room = GetPhotonCurrentRoom();
            if (room == null) { error = "not currently in a room"; return false; }
            try
            {
                Type roomType = room.GetType();
                PropertyInfo maxProp = roomType.GetProperty("MaxPlayers", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                PropertyInfo countProp = roomType.GetProperty("PlayerCount", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                if (maxProp == null) { error = "MaxPlayers property not found"; return false; }

                int currentPlayers = countProp != null ? (byte)countProp.GetValue(room) : 0;
                int clamped = Math.Max(requested, Math.Max(currentPlayers, 2));
                clamped = Math.Min(clamped, 255);
                maxProp.SetValue(room, (byte)clamped);
                applied = clamped;
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to set max players: {ex}");
                return false;
            }
        }

        private static bool TryLeaveCurrentRoom()
        {
            try
            {
                Type type = GetPhotonNetworkType();
                MethodInfo leaveMethod = type?.GetMethod("LeaveRoom", BindingFlags.Public | BindingFlags.Static, null, new[] { typeof(bool) }, null);
                if (leaveMethod == null) return false;
                leaveMethod.Invoke(null, new object[] { false });
                return true;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to call PhotonNetwork.LeaveRoom: {ex.Message}");
                return false;
            }
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
            SendChatMessage($"<color=#0000FF>[ADMIN]</color> Recreating room as private with name <color=#00FF88><i>{newName}</i></color>. Current players will be disconnected.");
            if (wasInRoom)
            {
                if (!TryLeaveCurrentRoom())
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Could not call LeaveRoom() — will rely on scene/menu recovery instead.");
                }
            }
        }

        // Keeps roomOwnedByBot in sync with the actual Photon master client, not just the bot's own
        // create/join history — this is the only way it becomes accurate again after a human
        // manually transfers host to the bot from Liftoff's player list following a by-name join.
        private static void HandleMasterClientSwitched(object newMasterClient)
        {
            try
            {
                if (newMasterClient == null) return;
                FieldInfo localField = newMasterClient.GetType().GetField("IsLocal", BindingFlags.Public | BindingFlags.Instance);
                bool isLocal = localField != null && (bool)localField.GetValue(newMasterClient);
                bool wasOwned = roomOwnedByBot;
                roomOwnedByBot = isLocal;

                if (isLocal && !wasOwned)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Master client switched to this bot — room is now bot-owned.");
                    QueueChatMessage("<color=#0000FF>[ADMIN]</color> This bot is now the room host — settings/rotation control restored.");
                }
                else if (!isLocal && wasOwned)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Master client switched away from this bot — room is no longer bot-owned.");
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception in HandleMasterClientSwitched: {ex}");
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
            if (errorCode == ErrorCode.GameIdAlreadyExists)
            {
                pendingPrivateRoomName = targetLobbyName;
                pendingJoinByName = true;
                pendingJoinByNameSetTime = DateTime.Now;
                joinByNamePanelSubmitted = false;
                joinByNameButtonClickedTime = DateTime.MinValue;
                QueueChatMessage($"<color=#0000FF>[ADMIN]</color> A room named '<color=#00FF88><i>{pendingPrivateRoomName}</i></color>' already exists — attempting to join it instead.");
            }
            else if (pendingPrivateRoomRename)
            {
                QueueChatMessage($"<color=#0000FF>[ADMIN]</color> Failed to create private room '<color=#00FF88><i>{pendingPrivateRoomName}</i></color>': {message}");
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
            if (errorCode == ErrorCode.GameFull || errorCode == ErrorCode.GameClosed || errorCode == ErrorCode.GameDoesNotExist)
            {
                FallBackToPublicRoom($"Room '{pendingPrivateRoomName}' couldn't be joined ({message}).");
            }
            else
            {
                QueueChatMessage($"<color=#0000FF>[ADMIN]</color> Failed to join room '<color=#00FF88><i>{pendingPrivateRoomName}</i></color>': {message}. Giving up.");
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
            QueueChatMessage($"<color=#0000FF>[ADMIN]</color> {reasonForChat} Falling back to a public room.");
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

        private static InputField FindInputFieldByName(string[] targetNames, string placeholderSubstring = null)
        {
            InputField[] fields = Resources.FindObjectsOfTypeAll<InputField>();
            foreach (InputField f in fields)
            {
                if (f == null || !f.gameObject.activeInHierarchy) continue;
                foreach (string name in targetNames)
                {
                    if (f.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return f;
                }
            }
            // Fallback: allow inactive match too, in case the panel hasn't been SetActive(true) yet
            foreach (InputField f in fields)
            {
                if (f == null) continue;
                foreach (string name in targetNames)
                {
                    if (f.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return f;
                }
            }
            // Fallback: match by placeholder text, in case the field's GameObject name changes
            // between game versions (name-based lookup already proved brittle once here).
            if (!string.IsNullOrEmpty(placeholderSubstring))
            {
                foreach (InputField f in fields)
                {
                    if (f == null || f.placeholder == null) continue;
                    Text pt = f.placeholder as Text;
                    if (pt != null && pt.text != null &&
                        pt.text.IndexOf(placeholderSubstring, StringComparison.OrdinalIgnoreCase) >= 0)
                        return f;
                }
            }
            return null;
        }

        // Scoped lookup: searches only within root's hierarchy, to avoid matching one of many
        // identically-named per-row buttons in a room list (e.g. "buttonJoin" appears once per
        // visible public room row, so a global scene-wide search is ambiguous).
        private static Button FindButtonInHierarchy(Transform root, string[] targetNames)
        {
            if (root == null) return null;
            foreach (Button btn in root.GetComponentsInChildren<Button>(true))
            {
                if (btn == null) continue;
                foreach (string name in targetNames)
                {
                    if (btn.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return btn;
                }
            }
            return null;
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

        private static bool GetPhotonIsConnectedAndReady()
        {
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type != null)
                {
                    PropertyInfo prop = type.GetProperty("IsConnectedAndReady", BindingFlags.Public | BindingFlags.Static);
                    if (prop != null)
                    {
                        return (bool)prop.GetValue(null);
                    }
                }
            }
            catch {}
            return false;
        }

        private static bool GetPhotonBoolProperty(string propertyName)
        {
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type != null)
                {
                    PropertyInfo prop = type.GetProperty(propertyName, BindingFlags.Public | BindingFlags.Static);
                    if (prop != null)
                    {
                        return (bool)prop.GetValue(null);
                    }
                }
            }
            catch {}
            return false;
        }

        private static void DumpButtonListeners(string name, Button btn)
        {
            if (btn == null)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Button '{name}' is null.");
                return;
            }
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Button '{name}': active={btn.gameObject.activeSelf}, activeInHierarchy={btn.gameObject.activeInHierarchy}, interactable={btn.interactable}");
            var onClick = btn.onClick;
            if (onClick == null)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin]   onClick is null.");
                return;
            }
            int persistentCount = onClick.GetPersistentEventCount();
            UnityEngine.Debug.Log($"[AutoLobbyPlugin]   onClick has {persistentCount} persistent listeners:");
            for (int i = 0; i < persistentCount; i++)
            {
                var target = onClick.GetPersistentTarget(i);
                var methodName = onClick.GetPersistentMethodName(i);
                UnityEngine.Debug.Log($"[AutoLobbyPlugin]     Persistent {i}: Target={target?.GetType().FullName}, Method={methodName}");
            }
        }

        private static void HandleChatCommand(string userName, string userId, string cmdText)
        {
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Processing command from {userName} ({userId}): {cmdText}");
            try
            {
                string[] parts = cmdText.Split(new char[]{' '}, 2, StringSplitOptions.RemoveEmptyEntries);
                string cmd = parts[0].ToLower();
                string arg = parts.Length > 1 ? parts[1].Trim() : "";

                if (cmd == "/info")
                {
                    string currentPlaylist = "all_official_races";
                    string playlistPath = Path.Combine(pluginPath, "playlist_name.txt");
                    if (File.Exists(playlistPath))
                        currentPlaylist = File.ReadAllText(playlistPath).Trim();

                    double rotationInterval = GetRotationInterval();
                    double elapsed = roomCreatedTime != DateTime.MinValue && roomCreatedTime != DateTime.MaxValue
                        ? (DateTime.Now - roomCreatedTime).TotalSeconds : 0;
                    double remaining = Math.Max(0, rotationInterval - elapsed);

                    string nextEnv, nextMode;
                    int trackIdx;
                    string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx);

                    string response = $"<color=#0000FF>[INFO]</color> Playlist: <color=#00FF88><i>{currentPlaylist}</i></color> | Interval: <color=#00FF88><i>{rotationInterval:F0}s</i></color> | Next in: <color=#00FF88><i>{remaining:F0}s</i></color> | Next: <color=#00FF88><i>{nextEnv} - {nextTrackName}</i></color> ";
                    SendChatMessage(response);

                    bool isVisible; string roomName; int maxPlayers; int playerCount;
                    if (TryGetRoomInfo(out isVisible, out roomName, out maxPlayers, out playerCount))
                    {
                        string visibility = isVisible ? "public" : "private";
                        string ownership = roomOwnedByBot ? "bot-owned" : "<color=#FF0000>NOT bot-owned — settings/rotation unavailable</color>";
                        string roomInfo = $"<color=#0000FF>[INFO]</color> Room: <color=#00FF88><i>{roomName}</i></color> | Visibility: <color=#00FF88><i>{visibility}</i></color> | Players: <color=#00FF88><i>{playerCount}/{maxPlayers}</i></color> | {ownership}";
                        SendChatMessage(roomInfo);
                    }
                    return;
                }

                // All other commands are admin-only — silently ignore non-admins to prevent probing
                if (!IsAdmin(userId))
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Ignoring command '{cmd}' from non-admin {userName} ({userId})");
                    return;
                }

                // Commands that mutate room state require the bot to actually be the room's master
                // client — Photon/Liftoff silently ignore property writes from non-master clients,
                // which previously made these commands look like they succeeded (e.g. /skip printing
                // "Skipping to next track." while nothing actually changed). /private <name> is
                // exempt: it leaves and creates/joins a different room regardless of current
                // ownership — that's the actual recovery path out of this situation.
                bool requiresOwnership = cmd == "/skip" || cmd == "/interval" || cmd == "/extend" ||
                    cmd == "/shuffle" || cmd == "/playlist" || cmd == "/mode" || cmd == "/kick" ||
                    cmd == "/maxplayers" || cmd == "/public" ||
                    (cmd == "/private" && string.IsNullOrWhiteSpace(arg));

                if (requiresOwnership && !roomOwnedByBot)
                {
                    SendChatMessage($"<color=#0000FF>[ADMIN]</color> <color=#FF0000>'{cmd}' cannot be executed — this bot does not own the room.</color> Transfer host to the bot from the player list, or use /private <name> to have it create/join a different room.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Refusing '{cmd}' from {userName} — bot does not own the room.");
                    return;
                }

                switch (cmd)
                {
                    case "/skip":
                        skipRequested = true;
                        chatWarnedAboutNextRace = false;
                        SendChatMessage("[ADMIN] Skipping to next track.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /skip");
                        break;

                    case "/interval":
                        double newInterval;
                        if (double.TryParse(arg, out newInterval) && newInterval >= 30.0)
                        {
                            File.WriteAllText(Path.Combine(pluginPath, "rotation_interval.txt"), newInterval.ToString("F0"));
                            SendChatMessage($"[ADMIN] Interval set to {newInterval:F0}s.");
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set interval to {newInterval}s");
                        }
                        else
                        {
                            SendChatMessage("[ADMIN] Usage: /interval <seconds> (min 30)");
                        }
                        break;

                    case "/extend":
                        double extendSecs;
                        if (double.TryParse(arg, out extendSecs) && extendSecs > 0)
                        {
                            if (roomCreatedTime != DateTime.MinValue && roomCreatedTime != DateTime.MaxValue)
                            {
                                roomCreatedTime = roomCreatedTime.AddSeconds(extendSecs);
                                double newRemaining = Math.Max(0, GetRotationInterval() - (DateTime.Now - roomCreatedTime).TotalSeconds);
                                chatWarnedAboutNextRace = false;
                                SendChatMessage($"[ADMIN] Extended by {extendSecs:F0}s. Next rotation in {newRemaining:F0}s.");
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} extended timer by {extendSecs}s");
                            }
                            else
                            {
                                SendChatMessage("[ADMIN] No active rotation timer.");
                            }
                        }
                        else
                        {
                            SendChatMessage("[ADMIN] Usage: /extend <seconds>");
                        }
                        break;

                    case "/shuffle":
                        if (arg.Equals("on", StringComparison.OrdinalIgnoreCase))
                        {
                            shuffleMode = true;
                            try
                            {
                                File.WriteAllText(Path.Combine(pluginPath, "shuffle_mode.txt"), "true");
                            }
                            catch (Exception ex)
                            {
                                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write shuffle_mode.txt: {ex.Message}");
                            }
                            SendChatMessage("[ADMIN] Shuffle on.");
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} enabled shuffle");
                        }
                        else if (arg.Equals("off", StringComparison.OrdinalIgnoreCase))
                        {
                            shuffleMode = false;
                            try
                            {
                                File.WriteAllText(Path.Combine(pluginPath, "shuffle_mode.txt"), "false");
                            }
                            catch (Exception ex)
                            {
                                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write shuffle_mode.txt: {ex.Message}");
                            }
                            SendChatMessage("[ADMIN] Shuffle off.");
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} disabled shuffle");
                        }
                        else
                        {
                            SendChatMessage("[ADMIN] Usage: /shuffle on|off");
                        }
                        break;

                    case "/playlist":
                        if (string.IsNullOrEmpty(arg))
                        {
                            string current = "";
                            string playlistPath = Path.Combine(pluginPath, "playlist_name.txt");
                            if (File.Exists(playlistPath)) current = File.ReadAllText(playlistPath).Trim();
                            SendChatMessage($"<color=#0000FF>[ADMIN]</color> Current playlist: <color=#00FF88><i>{current}</i></color>. Available: <color=#00FF88><i>{GetAvailablePlaylistsString()}</i></color>");
                        }
                        else if (PlaylistExists(arg))
                        {
                            try
                            {
                                File.WriteAllText(Path.Combine(pluginPath, "playlist_name.txt"), arg.Trim());
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Playlist set to <color=#00FF88><i>{arg.Trim()}</i></color>. Next track will be from the new playlist.");
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set playlist to {arg}");
                            }
                            catch (Exception ex)
                            {
                                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write playlist_name.txt: {ex.Message}");
                                SendChatMessage("<color=#0000FF>[ADMIN]</color> Failed to change playlist due to internal error.");
                            }
                        }
                        else
                        {
                            SendChatMessage($"<color=#0000FF>[ADMIN]</color> Unknown playlist. Available: <color=#00FF88><i>{GetAvailablePlaylistsString()}</i></color>");
                        }
                        break;

                    case "/mode":
                        if (string.IsNullOrEmpty(arg))
                        {
                            string currentMode = GetOverrideGameMode();
                            if (string.IsNullOrEmpty(currentMode)) currentMode = "auto (playlist default)";
                            SendChatMessage($"<color=#0000FF>[ADMIN]</color> Current mode: <color=#00FF88><i>{currentMode}</i></color>. Usage: /mode infinite|circuit|dropout|survival|auto");
                        }
                        else
                        {
                            string targetMode = "";
                            string lowerArg = arg.Trim().ToLower();
                            if (lowerArg == "infinite") targetMode = "Infinite Race";
                            else if (lowerArg == "circuit" || lowerArg == "classic") targetMode = "Classic Race";
                            else if (lowerArg == "dropout") targetMode = "Dropout Race";
                            else if (lowerArg == "survival") targetMode = "Survival";
                            else if (lowerArg == "auto" || lowerArg == "off" || lowerArg == "reset") targetMode = "auto";

                            if (targetMode == "")
                            {
                                SendChatMessage("<color=#0000FF>[ADMIN]</color> Invalid mode. Supported: infinite, circuit, dropout, survival, auto");
                            }
                            else if (targetMode == "auto")
                            {
                                try
                                {
                                    string path = Path.Combine(pluginPath, "override_game_mode.txt");
                                    if (File.Exists(path)) File.Delete(path);
                                    SendChatMessage("<color=#0000FF>[ADMIN]</color> Game mode reset to <color=#00FF88><i>playlist default</i></color>.");
                                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} reset override game mode to auto.");
                                }
                                catch (Exception ex)
                                {
                                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to delete override_game_mode.txt: {ex.Message}");
                                }
                            }
                            else
                            {
                                try
                                {
                                    File.WriteAllText(Path.Combine(pluginPath, "override_game_mode.txt"), targetMode);
                                    SendChatMessage($"<color=#0000FF>[ADMIN]</color> Game mode set to <color=#00FF88><i>{targetMode}</i></color>.");
                                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set override game mode to {targetMode}.");
                                }
                                catch (Exception ex)
                                {
                                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write override_game_mode.txt: {ex.Message}");
                                }
                            }
                        }
                        break;

                    case "/kick":
                        if (string.IsNullOrEmpty(arg))
                        {
                            SendChatMessage("<color=#0000FF>[ADMIN]</color> Usage: /kick <player_name>");
                        }
                        else
                        {
                            string matchedName;
                            string matchesList;
                            if (KickPlayer(arg, out matchedName, out matchesList))
                            {
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Kicked player <color=#00FF88><i>{matchedName}</i></color>.");
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} kicked player {matchedName}");
                            }
                            else if (matchedName == "multiple")
                            {
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Multiple matches found: <color=#00FF88><i>{matchesList}</i></color>. Please be more specific.");
                            }
                            else
                            {
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> No player found matching <color=#00FF88><i>'{arg}'</i></color>.");
                            }
                        }
                        break;

                    case "/maintenance":
                        if (!string.IsNullOrEmpty(arg) && arg.Equals("cancel", StringComparison.OrdinalIgnoreCase))
                        {
                            CancelMaintenance();
                            SendChatMessage("<color=#0000FF>[ADMIN]</color> Scheduled maintenance cancelled.");
                        }
                        else
                        {
                            double mins = 5.0;
                            if (!string.IsNullOrEmpty(arg))
                            {
                                double.TryParse(arg, out mins);
                            }
                            if (mins <= 0) mins = 5.0;

                            maintenanceActive = true;
                            maintenanceTime = DateTime.Now.AddMinutes(mins);
                            lastMaintenanceWarningMinutes = -1;
                            maintenanceWarning30sSent = false;
                            maintenanceWarning10sSent = false;
                            try
                            {
                                File.WriteAllText(Path.Combine(pluginPath, "maintenance_active.txt"), "true");
                            }
                            catch (Exception ex)
                            {
                                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write maintenance_active.txt: {ex.Message}");
                            }
                            SendChatMessage($"<color=#0000FF>[ADMIN]</color> Shutdown for maintenance scheduled in <color=#00FF88><i>{mins:F1}m</i></color>.");
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} scheduled maintenance in {mins} minutes.");
                        }
                        break;

                    case "/private":
                        if (pendingPrivateRoomRename)
                        {
                            SendChatMessage("<color=#0000FF>[ADMIN]</color> A room rename is already in progress. Please wait for it to finish.");
                        }
                        else if (string.IsNullOrWhiteSpace(arg))
                        {
                            // No name given: just toggle visibility on the current room, name unchanged.
                            string curName;
                            string setErr;
                            if (SetRoomVisibility(true, out curName, out setErr))
                            {
                                try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "true"); } catch { }
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Room is now private. Join name: <color=#00FF88><i>{curName}</i></color>.");
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set room to private.");
                            }
                            else
                            {
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Could not change visibility: {setErr}. Usage: /private [name] (name recreates the room with that join name).");
                            }
                        }
                        else
                        {
                            BeginPrivateRoomRename(arg.Trim(), userName);
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} requested private room rename to '{arg.Trim()}'.");
                        }
                        break;

                    case "/public":
                        {
                            string curName;
                            string setErr;
                            if (SetRoomVisibility(false, out curName, out setErr))
                            {
                                try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "false"); } catch { }
                                SendChatMessage("<color=#0000FF>[ADMIN]</color> Room is now public.");
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set room to public.");
                            }
                            else
                            {
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Could not change visibility: {setErr}.");
                            }
                        }
                        break;

                    case "/maxplayers":
                        int requestedMax;
                        if (!int.TryParse(arg, out requestedMax) || requestedMax < 2)
                        {
                            SendChatMessage("<color=#0000FF>[ADMIN]</color> Usage: /maxplayers <number> (min 2)");
                        }
                        else
                        {
                            int applied;
                            string setErr;
                            if (SetRoomMaxPlayers(requestedMax, out applied, out setErr))
                            {
                                try { File.WriteAllText(Path.Combine(pluginPath, "max_players.txt"), applied.ToString()); } catch { }
                                string clampNote = applied != requestedMax ? $" (clamped from {requestedMax})" : "";
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Max players set to <color=#00FF88><i>{applied}</i></color>{clampNote}.");
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set max players to {applied} (requested {requestedMax}).");
                            }
                            else
                            {
                                SendChatMessage($"<color=#0000FF>[ADMIN]</color> Could not set max players: {setErr}.");
                            }
                        }
                        break;

                    default:
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Unknown admin command '{cmd}' from {userName}");
                        break;
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in HandleChatCommand: {ex}");
            }
        }

        private static bool IsDuplicateMessage(string userName, string message)
        {
            try
            {
                // Clean up messages older than 5 seconds
                processedMessages.RemoveAll(m => (DateTime.Now - m.Item3).TotalSeconds > 5.0);

                // Check if this combination of userName + message was processed recently
                foreach (var pm in processedMessages)
                {
                    if (pm.Item1 == userName && pm.Item2 == message)
                    {
                        return true;
                    }
                }

                processedMessages.Add(new Tuple<string, string, DateTime>(userName, message, DateTime.Now));
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in IsDuplicateMessage: {ex}");
            }
            return false;
        }

        [HarmonyPatch]
        class ChatMessagePatch
        {
            public static MethodBase TargetMethod()
            {
                Type chatType = FindType("Liftoff.Multiplayer.Chat.ChatWindowPanel");
                if (chatType == null) return null;
                return chatType.GetMethod("GenerateUserMessage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance, null, new Type[] { typeof(string), typeof(string), typeof(string), typeof(UnityEngine.Color) }, null);
            }

            private static bool IsRenderingHistory()
            {
                try
                {
                    string stack = System.Environment.StackTrace;
                    return stack.IndexOf("GenerateChatFromHistory", StringComparison.OrdinalIgnoreCase) >= 0;
                }
                catch
                {
                    return false;
                }
            }

            public static void Postfix(string userId, string userName, string message, UnityEngine.Color ledColor)
            {
                try
                {
                    if (message == null) return;
                    if (IsRenderingHistory()) return;

                    string trimmedMsg = message.Trim();
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Chat received from {userName} (ID: {userId}): {trimmedMsg}");
                    
                    if (trimmedMsg.StartsWith("/"))
                    {
                        if (IsDuplicateMessage(userName, trimmedMsg))
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Ignoring duplicate command '{trimmedMsg}' from {userName}");
                            return;
                        }
                        HandleChatCommand(userName, userId, trimmedMsg);
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in ChatMessagePatch: {ex}");
                }
            }
        }
    }
}
