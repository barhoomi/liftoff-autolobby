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
        private static bool databaseDumped = false;
        private static int databaseDumpRetries = 0;
        private static DateTime lastDatabaseDumpTime = DateTime.MinValue;
        private static bool liftoffProLoginAttempted = false;
        private static DateTime liftoffProLoginClickTime = DateTime.MinValue;
        private static DateTime lastSignInClickTime = DateTime.MinValue;
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

            if (!databaseDumped && databaseDumpRetries < 3 && (DateTime.Now - lastDatabaseDumpTime).TotalSeconds >= 120.0)
            {
                if (sceneName == "MainMenu" || sceneName == "MultiplayerMenu")
                {
                    lastDatabaseDumpTime = DateTime.Now;
                    databaseDumpRetries++;
                    DumpGameDatabase();
                    if (databaseDumpRetries >= 3)
                    {
                        UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Reached maximum database dump attempts. Stopping dump retries to prevent performance degradation.");
                        databaseDumped = true;
                    }
                }
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

                // Click with a 30s cooldown — auth takes time to process server-side
                if ((DateTime.Now - lastSignInClickTime).TotalSeconds > 30.0)
                {
                    lastSignInClickTime = DateTime.Now;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking center sign-in button: name='{bestBtn.name}' text='{GetButtonText(bestBtn)}' pos={bestBtn.transform.position}");
                    bestBtn.onClick.Invoke();
                }
                else if (DateTime.Now.Second % 5 == 0)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for sign-in response ({(DateTime.Now - lastSignInClickTime).TotalSeconds:F0}s / 30s)...");
                }

                // After 60s with no progress, go back to MainMenu to reset state
                if (timeSinceLoad > 60.0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Still on sign-in screen after 60s — returning to MainMenu.");
                    liftoffProLoginAttempted = false;
                    liftoffProLoginClickTime = DateTime.MinValue;
                    NavigateToMainMenu();
                }
                return;
            }

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
            double timeInMenu = (DateTime.Now - sceneLoadTime).TotalSeconds;
            double timeSinceRoom = lastInRoomTime != DateTime.MinValue
                ? (DateTime.Now - lastInRoomTime).TotalSeconds
                : timeInMenu;

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

            // Grace period expired (or never been in a room this scene load) — reset state
            roomCreatedTime = DateTime.MinValue;
            isLeaving = false;

            // Stuck-in-menu fallback: only fires after grace period
            if (timeInMenu > 90.0 && timeSinceRoom > 120.0)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Stuck in MultiplayerMenu for {timeInMenu:F0}s (out of room for {timeSinceRoom:F0}s) — navigating back to MainMenu.");
                NavigateToMainMenu();
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

            if (roomCreatedTime == DateTime.MinValue || roomCreatedTime == DateTime.MaxValue)
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Entered GameRoom. Starting room timer.");
                roomCreatedTime = DateTime.Now;
                chatWarnedAboutNextRace = false;
                firstStartGameClickTime = DateTime.MinValue;
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
                    isDumpingTrackModes = true;
                    dumpEnvIndex2 = 0;
                    dumpModeIndex2 = 0;
                    dumpedTrackModeMap.Clear();
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Starting Environment x GameMode track availability dump ({TrackModeDumpCandidateModes.Length} candidate modes)...");
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
                    if (chats.Length > 0 && chats[0] != null)
                    {
                        MonoBehaviour chatPanel = (MonoBehaviour)chats[0];
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

        private static void FindDepotsRecursively(object obj, HashSet<object> visited, List<object> foundDepots, Type shareableType)
        {
            if (obj == null) return;
            if (visited.Contains(obj)) return;
            visited.Add(obj);

            Type t = obj.GetType();
            
            var fields = t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (fields == null) return;

            bool isDepot = false;
            foreach (var f in fields)
            {
                if (f != null && f.FieldType != null && f.FieldType.IsGenericType && f.FieldType.GetGenericTypeDefinition() == typeof(List<>))
                {
                    var genericArgs = f.FieldType.GetGenericArguments();
                    if (genericArgs != null && genericArgs.Length > 0 && genericArgs[0] != null)
                    {
                        if (shareableType.IsAssignableFrom(genericArgs[0]) || genericArgs[0] == shareableType)
                        {
                            isDepot = true;
                            break;
                        }
                    }
                }
            }

            if (isDepot)
            {
                if (!foundDepots.Contains(obj))
                {
                    foundDepots.Add(obj);
                }
                return;
            }

            foreach (var f in fields)
            {
                if (f != null && f.FieldType != null && f.FieldType.IsClass && f.FieldType != typeof(string) && !f.FieldType.IsPointer && !f.FieldType.IsPrimitive && !f.FieldType.IsValueType)
                {
                    try
                    {
                        object val = f.GetValue(obj);
                        if (val != null)
                        {
                            FindDepotsRecursively(val, visited, foundDepots, shareableType);
                        }
                    }
                    catch {}
                }
            }
        }

        private static void DumpGameDatabase()
        {
            try
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Starting game database dump...");
                Assembly asm = Assembly.Load("Assembly-CSharp");
                
                Type shareableType = asm.GetType("ShareableContent") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "ShareableContent");
                Type environmentType = asm.GetType("Environment") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "Environment");
                Type trackType = asm.GetType("TrackQuickInfo") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "TrackQuickInfo");
                Type raceType = asm.GetType("RaceQuickInfo") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "RaceQuickInfo");

                if (shareableType == null || environmentType == null || trackType == null || raceType == null)
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] DumpGameDatabase: Failed to resolve database types.");
                    return;
                }

                // 1. Find all environments (Environment inherits from UnityEngine.Object)
                UnityEngine.Object[] envObjects = Resources.FindObjectsOfTypeAll(environmentType);
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found {envObjects.Length} Environment objects in Resources.");

                // 2. Find all tracks and races by recursively scanning static fields in assembly
                var tracks = new List<object>();
                var races = new List<object>();
                var foundDepots = new List<object>();
                var visited = new HashSet<object>();

                UnityEngine.Debug.Log("[AutoLobbyPlugin] Scanning assembly for static depots...");
                foreach (Type t in asm.GetTypes())
                {
                    if (t == null || !t.IsClass) continue;
                    
                    var sf = t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                    if (sf == null) continue;

                    foreach (var f in sf)
                    {
                        if (f != null && f.FieldType.IsClass && f.FieldType != typeof(string))
                        {
                            try
                            {
                                object val = f.GetValue(null);
                                if (val != null)
                                {
                                    FindDepotsRecursively(val, visited, foundDepots, shareableType);
                                }
                            }
                            catch {}
                        }
                    }
                }

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found {foundDepots.Count} depot instances in memory.");

                foreach (var depotInstance in foundDepots)
                {
                    var fields = depotInstance.GetType().GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    if (fields == null) continue;

                    foreach (var f in fields)
                    {
                        if (f != null && f.FieldType != null && f.FieldType.IsGenericType && f.FieldType.GetGenericTypeDefinition() == typeof(List<>))
                        {
                            var genericArgs = f.FieldType.GetGenericArguments();
                            if (genericArgs != null && genericArgs.Length > 0 && genericArgs[0] != null)
                            {
                                if (shareableType.IsAssignableFrom(genericArgs[0]) || genericArgs[0] == shareableType)
                                {
                                    var listVal = f.GetValue(depotInstance);
                                    if (listVal != null)
                                    {
                                        var countProp = listVal.GetType().GetProperty("Count");
                                        if (countProp != null)
                                        {
                                            int count = (int)countProp.GetValue(listVal);
                                            var getItemMethod = listVal.GetType().GetMethod("get_Item");
                                            if (getItemMethod != null)
                                            {
                                                for (int i = 0; i < count; i++)
                                                {
                                                    object item = getItemMethod.Invoke(listVal, new object[] { i });
                                                    if (item != null)
                                                    {
                                                        if (trackType.IsAssignableFrom(item.GetType()))
                                                        {
                                                            if (!tracks.Contains(item)) tracks.Add(item);
                                                        }
                                                        else if (raceType.IsAssignableFrom(item.GetType()))
                                                        {
                                                            if (!races.Contains(item)) races.Add(item);
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Final counts: Environments: {envObjects.Length}, Tracks: {tracks.Count}, Races: {races.Count}");

                // 3. Build JSON representation
                var envList = new List<Dictionary<string, object>>();
                
                // Track name mapper to map track ID to track name
                var trackIdToName = new Dictionary<string, string>();
                var trackIdToEnv = new Dictionary<string, string>();

                foreach (var track in tracks)
                {
                    string tName = "";
                    var nameProp = track.GetType().GetProperty("Name");
                    if (nameProp != null) tName = (string)nameProp.GetValue(track) ?? "";

                    string envName = "";
                    var envField = track.GetType().GetField("environment", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    if (envField != null) envName = (string)envField.GetValue(track) ?? "";

                    string localIdStr = "";
                    var localIdProp = track.GetType().GetProperty("LocalID");
                    if (localIdProp != null)
                    {
                        object localIdVal = localIdProp.GetValue(track);
                        if (localIdVal != null) localIdStr = localIdVal.ToString() ?? "";
                    }

                    if (!string.IsNullOrEmpty(localIdStr))
                    {
                        trackIdToName[localIdStr] = tName;
                        trackIdToEnv[localIdStr] = envName;
                    }
                }

                // Group by Environment
                var envGroups = new Dictionary<string, List<Dictionary<string, object>>>();

                foreach (var track in tracks)
                {
                    string tName = "";
                    var nameProp = track.GetType().GetProperty("Name");
                    if (nameProp != null) tName = (string)nameProp.GetValue(track) ?? "";

                    string envName = "";
                    var envField = track.GetType().GetField("environment", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    if (envField != null) envName = (string)envField.GetValue(track) ?? "";
                    if (string.IsNullOrEmpty(envName)) envName = "Unknown";

                    string localIdStr = "";
                    var localIdProp = track.GetType().GetProperty("LocalID");
                    if (localIdProp != null)
                    {
                        object localIdVal = localIdProp.GetValue(track);
                        if (localIdVal != null) localIdStr = localIdVal.ToString() ?? "";
                    }

                    if (!envGroups.ContainsKey(envName))
                    {
                        envGroups[envName] = new List<Dictionary<string, object>>();
                    }

                    // Find races for this track
                    var trackRaces = new List<string>();
                    foreach (var race in races)
                    {
                        string rName = "";
                        var rNameProp = race.GetType().GetProperty("Name");
                        if (rNameProp != null) rName = (string)rNameProp.GetValue(race) ?? "";

                        string trackDepStr = "";
                        var trackDepProp = race.GetType().GetProperty("TrackDependency");
                        if (trackDepProp != null)
                        {
                            object trackDepVal = trackDepProp.GetValue(race);
                            if (trackDepVal != null) trackDepStr = trackDepVal.ToString() ?? "";
                        }

                        if (trackDepStr == localIdStr && !string.IsNullOrEmpty(rName))
                        {
                            trackRaces.Add(rName);
                        }
                    }

                    var trackDict = new Dictionary<string, object>
                    {
                        { "track_name", tName },
                        { "local_id", localIdStr },
                        { "races", trackRaces }
                    };

                    envGroups[envName].Add(trackDict);
                }

                foreach (var kvp in envGroups)
                {
                    string envDisplayName = kvp.Key;
                    
                    // Try to find the matching Environment object to get the display name
                    if (envObjects != null)
                    {
                        foreach (var envObj in envObjects)
                        {
                            if (envObj != null)
                            {
                                var nameVal = envObj.name ?? "";
                                var dispProp = envObj.GetType().GetProperty("DisplayName");
                                string dispVal = dispProp != null ? (string)dispProp.GetValue(envObj) ?? "" : "";
                                
                                if (nameVal == kvp.Key || dispVal == kvp.Key)
                                {
                                    envDisplayName = dispVal;
                                    break;
                                }
                            }
                        }
                    }

                    var envDict = new Dictionary<string, object>
                    {
                        { "environment_name", kvp.Key },
                        { "display_name", envDisplayName },
                        { "tracks", kvp.Value }
                    };
                    envList.Add(envDict);
                }

                // Serialize manually to avoid dependency on Newtonsoft.Json
                StringBuilder sb = new StringBuilder();
                sb.AppendLine("[");
                for (int i = 0; i < envList.Count; i++)
                {
                    var env = envList[i];
                    sb.AppendLine("  {");
                    sb.AppendLine($"    \"environment_name\": \"{EscapeJson(env["environment_name"].ToString())}\",");
                    sb.AppendLine($"    \"display_name\": \"{EscapeJson(env["display_name"].ToString())}\",");
                    sb.AppendLine("    \"tracks\": [");
                    
                    var envTracks = (List<Dictionary<string, object>>)env["tracks"];
                    for (int j = 0; j < envTracks.Count; j++)
                    {
                        var track = envTracks[j];
                        sb.AppendLine("      {");
                        sb.AppendLine($"        \"track_name\": \"{EscapeJson(track["track_name"].ToString())}\",");
                        sb.AppendLine($"        \"local_id\": \"{EscapeJson(track["local_id"].ToString())}\",");
                        sb.Append("        \"races\": [");
                        
                        var trackRaces = (List<string>)track["races"];
                        for (int k = 0; k < trackRaces.Count; k++)
                        {
                            sb.Append($"\"{EscapeJson(trackRaces[k])}\"");
                            if (k < trackRaces.Count - 1) sb.Append(", ");
                        }
                        sb.AppendLine("]");
                        
                        sb.Append("      }");
                        if (j < envTracks.Count - 1) sb.AppendLine(",");
                        else sb.AppendLine();
                    }
                    sb.AppendLine("    ]");
                    sb.Append("  }");
                    if (i < envList.Count - 1) sb.AppendLine(",");
                    else sb.AppendLine();
                }
                sb.AppendLine("]");

                if (tracks.Count > 0 && races.Count > 0)
                {
                    string dumpPath = Path.Combine(pluginPath, "liftoff_database_dump.json");
                    File.WriteAllText(dumpPath, sb.ToString());
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Game database successfully dumped to: {dumpPath}");
                    databaseDumped = true;
                }
                else
                {
                    if (DateTime.Now.Second % 10 == 0)
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] DumpGameDatabase: Database not ready yet (Environments: {envObjects.Length}, Tracks: {tracks.Count}, Races: {races.Count}). Retrying...");
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] DumpGameDatabase failed: {ex}");
            }
        }

        private static string EscapeJson(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
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
                    return;
                }

                // All other commands are admin-only — silently ignore non-admins to prevent probing
                if (!IsAdmin(userId))
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Ignoring command '{cmd}' from non-admin {userName} ({userId})");
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
