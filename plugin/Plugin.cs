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
        private static DateTime lastSignInClickTime = DateTime.MinValue;
        private static bool triedCustomContentTab = false;
        private static string lastSceneName = "";
        private static DateTime sceneLoadTime = DateTime.MinValue;
        private static bool isDumpingUI = false;
        private static int dumpEnvIndex = 0;
        private static Dictionary<string, List<string>> dumpedTracksMap = new Dictionary<string, List<string>>();
        private static List<Tuple<string, string, DateTime>> processedMessages = new List<Tuple<string, string, DateTime>>();

        private void Awake()
        {
            Logger.LogInfo("[AutoLobbyPlugin] BepInEx Awake called!");
            try
            {
                pluginPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "BepInEx", "plugins");
                
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
            string sceneName = SceneManager.GetActiveScene().name;
            if (sceneName != lastSceneName)
            {
                lastSceneName = sceneName;
                sceneLoadTime = DateTime.Now;
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Scene changed to: {sceneName}");

                // Reset room timer when loading into a flight level scene
                if (sceneName != "MainMenu" && sceneName != "MultiplayerMenu" &&
                    sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene")
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Level loaded. Resetting room timer.");
                    roomCreatedTime = DateTime.Now;
                    isLeaving = false;
                }
            }

            // Log status every 30 seconds for visibility
            if (DateTime.Now.Second % 30 == 0)
            {
                double elapsed = roomCreatedTime != DateTime.MinValue ? (DateTime.Now - roomCreatedTime).TotalSeconds : 0;
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Tick running. Scene: {sceneName}, Room timer elapsed: {elapsed:F1}s / {GetRotationInterval()}s");
            }

            if (!databaseDumped)
            {
                if (sceneName == "MainMenu" || sceneName == "MultiplayerMenu")
                {
                    DumpGameDatabase();
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

        private static void HandleMainMenu()
        {
            // Reset state
            roomCreatedTime = DateTime.MinValue;
            isLeaving = false;

            // 1. First, check if the Lobby button is active and click it
            string[] lobbyNames = { "MultiplayerLobby", "btnMultiplayerLobby" };
            Button lobbyBtn = FindButtonByTextOrName("LOBBY", lobbyNames);

            if (lobbyBtn != null && lobbyBtn.gameObject.activeInHierarchy && lobbyBtn.interactable)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Lobby button is active. Clicking it: {lobbyBtn.name}");
                lobbyBtn.onClick.Invoke();
                return;
            }

            // 2. If Lobby button is not active, click the MULTIPLAYER category button
            string[] categoryNames = { "BtnHeading", "Multiplayer" };
            Button categoryBtn = FindButtonByTextOrName("MULTIPLAYER", categoryNames);

            if (categoryBtn != null)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking Multiplayer category button to open sub-menu: {categoryBtn.name}");
                categoryBtn.onClick.Invoke();
            }
        }

        private static bool sceneObjectsDumped = false;

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

        private static void HandleMultiplayerMenu()
        {
            DumpActiveSceneObjects();

            // 1. Connect if on the SignIn panel
            string[] signInNames = { "buttonSignInAnonymous", "btnSignInAnonymous", "SignInAnonymous" };
            Button signInBtn = FindButtonByTextOrName("SIGN IN ANONYMOUSLY", signInNames);
            if (signInBtn == null)
            {
                // Try text containing "anonymous"
                Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
                foreach (var b in buttons)
                {
                    if (b == null) continue;
                    string txt = GetButtonText(b);
                    if (txt.IndexOf("anonymous", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        signInBtn = b;
                        break;
                    }
                }
            }

            if (signInBtn != null && signInBtn.gameObject.activeInHierarchy && signInBtn.interactable)
            {
                // Wait 10 seconds after entering MultiplayerMenu before clicking Sign In
                // to allow any automatic login to proceed
                double timeSinceLoad = (DateTime.Now - sceneLoadTime).TotalSeconds;
                if (timeSinceLoad < 10.0)
                {
                    if (DateTime.Now.Second % 5 == 0)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Waiting for auto-login (elapsed: {timeSinceLoad:F1}s / 10s)...");
                    }
                    return;
                }

                if ((DateTime.Now - lastSignInClickTime).TotalSeconds > 10.0)
                {
                    lastSignInClickTime = DateTime.Now;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Clicking Anonymous Sign In button: {signInBtn.name}");
                    signInBtn.onClick.Invoke();
                }
                else
                {
                    if (DateTime.Now.Second % 5 == 0)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Sign In click cooldown active, waiting for connection...");
                    }
                }
                return;
            }

            // 2. Detect Liftoff Pro session-expired login page
            // These buttons appear when the Pro account token lapses and the game shows a re-login screen.
            string[] skipLoginNames = { "btnSkip", "buttonSkip", "btnContinue", "buttonContinue", "btnPlayWithout", "btnGuest" };
            Button skipLoginBtn = FindButtonByTextOrName("CONTINUE", skipLoginNames);
            if (skipLoginBtn == null) skipLoginBtn = FindButtonByTextOrName("SKIP", skipLoginNames);
            if (skipLoginBtn == null) skipLoginBtn = FindButtonByTextOrName("PLAY WITHOUT LIFTOFF PRO", skipLoginNames);
            if (skipLoginBtn == null) skipLoginBtn = FindButtonByTextOrName("PLAY AS GUEST", skipLoginNames);
            if (skipLoginBtn != null && skipLoginBtn.gameObject.activeInHierarchy && skipLoginBtn.interactable)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Liftoff Pro login page detected. Clicking: '{GetButtonText(skipLoginBtn)}' ({skipLoginBtn.name})");
                skipLoginBtn.onClick.Invoke();
                return;
            }

            // 2b. Stuck-in-menu fallback: if we've been here >90s with no actionable button, force back to MainMenu
            double timeInMenu = (DateTime.Now - sceneLoadTime).TotalSeconds;
            if (timeInMenu > 90.0)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Stuck in MultiplayerMenu for {timeInMenu:F0}s — navigating back to MainMenu.");
                SceneManager.LoadScene("MainMenu");
                return;
            }

            // 3. Check if GameRoom is active
            GameObject gameRoomObj = GameObject.Find("GameRoom");
            bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy);

            if (inRoom)
            {
                HandleGameRoom();
                return;
            }
            else
            {
                // Not in room, reset room timer
                roomCreatedTime = DateTime.MinValue;
                isLeaving = false;
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
                    startBtn.onClick.Invoke();
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

            if (elapsed >= GetRotationInterval())
            {
                // Timer expired inside waiting room, open change settings popup!
                string[] changeSettingsNames = { "buttonChangeRoomSettings", "btnChangeRoomSettings", "ChangeRoomSettings" };
                Button changeSettingsBtn = FindButtonByTextOrName("CHANGE SETTINGS", changeSettingsNames);
                if (changeSettingsBtn != null && changeSettingsBtn.gameObject.activeInHierarchy && changeSettingsBtn.interactable)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Timer expired in waiting room. Clicking CHANGE SETTINGS button.");
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
                string dumpFilePath = Path.Combine(pluginPath, "ui_tracks_dump.json");
                if (!isDumpingUI && !File.Exists(dumpFilePath))
                {
                    isDumpingUI = true;
                    dumpEnvIndex = 0;
                    dumpedTracksMap.Clear();
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Starting UI track dump...");
                }

                if (isDumpingUI)
                {
                    LiftoffDropdown dropdownEnvironment = GetContentDropdownEnvironment(contentSettings);
                    LiftoffDropdown dropdownContent = GetContentDropdownContent(contentSettings);
                    if (dropdownEnvironment != null && dropdownContent != null)
                    {
                        if (dumpEnvIndex < dropdownEnvironment.options.Count)
                        {
                            if (dropdownEnvironment.value != dumpEnvIndex)
                            {
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] UI Dump: Selecting environment index {dumpEnvIndex} ({dropdownEnvironment.options[dumpEnvIndex].text})");
                                dropdownEnvironment.value = dumpEnvIndex;
                                dropdownEnvironment.onValueChanged.Invoke(dumpEnvIndex);
                                return; // Let the UI update next frame
                            }

                            string envName = dropdownEnvironment.options[dumpEnvIndex].text;
                            var tracks = new List<string>();
                            for (int i = 0; i < dropdownContent.options.Count; i++)
                            {
                                tracks.Add(dropdownContent.options[i].text);
                            }
                            dumpedTracksMap[envName] = tracks;
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] UI Dump: Collected {tracks.Count} tracks for environment '{envName}'");

                            dumpEnvIndex++;
                            return; // Wait for next tick
                        }
                        else
                        {
                            try
                            {
                                List<string> lines = new List<string>();
                                lines.Add("{");
                                int count = 0;
                                foreach (var kvp in dumpedTracksMap)
                                {
                                    count++;
                                    List<string> trackListStr = new List<string>();
                                    foreach (var track in kvp.Value)
                                    {
                                        trackListStr.Add($"\"{track.Replace("\"", "\\\"")}\"");
                                    }
                                    string comma = (count < dumpedTracksMap.Count) ? "," : "";
                                    lines.Add($"  \"{kvp.Key.Replace("\"", "\\\"")}\": [{string.Join(", ", trackListStr.ToArray())}]{comma}");
                                }
                                lines.Add("}");
                                File.WriteAllLines(dumpFilePath, lines.ToArray());
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] UI Dump successful! Saved to: {dumpFilePath}");
                            }
                            catch (Exception dumpEx)
                            {
                                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write UI dump: {dumpEx}");
                            }
                            isDumpingUI = false;
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
                InputField inputRoomName = GetRoomSettingsInputField(roomSettings);
                if (inputRoomName != null)
                {
                    inputRoomName.text = targetLobbyName;
                }
                Toggle togglePrivate = GetRoomSettingsTogglePrivate(roomSettings);
                if (togglePrivate != null)
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
                    togglePrivate.isOn = makePrivate;
                }
            }

            // 2. Configure Content settings (GameMode, Environment, Track)
            contentSettings = GetPopupContentSettings(popup);
            if (contentSettings != null)
            {
                // Set GameMode
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

        private static void SendChatMessage(string message)
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

                int index = 0;
                if (File.Exists(statePath))
                {
                    int.TryParse(File.ReadAllText(statePath).Trim(), out index);
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Current state index: {index}, validTracks count: {validTracks.Count}");

                if (index < 0 || index >= validTracks.Count)
                {
                    index = 0;
                }

                trackIndex = index;
                string selectedLine = validTracks[index];
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Selected line: '{selectedLine}'");
                
                string[] parts = selectedLine.Split(',');
                string trackName = parts[0].Trim();
                if (parts.Length > 1) environment = parts[1].Trim();
                if (parts.Length > 2) gameMode = parts[2].Trim();

                return trackName;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PeekNextTrackName: {ex.Message}");
                return "";
            }
        }

        private static string GetNextTrackFromRotation(out string environment, out string gameMode)
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

                int index = 0;
                if (File.Exists(statePath))
                {
                    int.TryParse(File.ReadAllText(statePath).Trim(), out index);
                }

                if (index < 0 || index >= validTracks.Count)
                {
                    index = 0;
                }

                string selectedLine = validTracks[index];
                
                // Write next index for next rotation
                int nextIndex = (index + 1) % validTracks.Count;
                File.WriteAllText(statePath, nextIndex.ToString());

                // Parse line: TrackName,EnvironmentName,GameModeName
                string[] parts = selectedLine.Split(',');
                string trackName = parts[0].Trim();
                if (parts.Length > 1) environment = parts[1].Trim();
                if (parts.Length > 2) gameMode = parts[2].Trim();

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
                    string[] callbackContainerTypes = new string[]
                    {
                        "Photon.Realtime.InRoomCallbacksContainer"
                    };

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
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Processing command from {userName}: {cmdText}");
            try
            {
                string[] parts = cmdText.Split(' ');
                string cmd = parts[0].ToLower();

                if (cmd == "/info")
                {
                    string currentPlaylist = "all_official_races";
                    string playlistPath = Path.Combine(pluginPath, "playlist_name.txt");
                    if (File.Exists(playlistPath))
                    {
                        currentPlaylist = File.ReadAllText(playlistPath).Trim();
                    }

                    double rotationInterval = GetRotationInterval();
                    double elapsed = roomCreatedTime != DateTime.MinValue && roomCreatedTime != DateTime.MaxValue 
                        ? (DateTime.Now - roomCreatedTime).TotalSeconds 
                        : 0;
                    
                    double remaining = rotationInterval - elapsed;
                    if (remaining < 0) remaining = 0;

                    string nextEnv, nextMode;
                    int trackIdx;
                    string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx);

                    string response = $"<color=#0000FF>[INFO]</color> Playlist: <color=#00FF88><i>{currentPlaylist}</i></color> | Interval: <color=#00FF88><i>{rotationInterval:F0}s</i></color> | Next in: <color=#00FF88><i>{remaining:F0}s</i></color> | Next: <color=#00FF88><i>{nextEnv} - {nextTrackName}</i></color>";
                    SendChatMessage(response);
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
