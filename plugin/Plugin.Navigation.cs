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
    // Menu automation and sign-in: MainMenu -> MultiplayerMenu navigation, Liftoff Pro
    //     sign-in (default and distinct-credentials paths), anonymous skip, and
    //     MultiplayerMenu UI-state logging.
    public partial class AutoLobbyPlugin
    {


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

        // Confirmed live 2026-07-02: the real anonymous-login button on the
        // MultiplayerMenu sign-in screen is
        // Menu/SignIn/MultiplayerSignIn/panelSignInForm/Content/panelLoginAnonymous/buttonSignInAnonymous,
        // with visible TEXT just "Connect" — "anonymous" only appears in its name, not its
        // label. Matching on text alone (skip/guest/anonymous/without, the original guess)
        // found nothing and fell through to the credentialed-recovery path below, which
        // clicked "Sign in" instead and got stuck waiting on a response that never comes.
        // Match on name first (reliable, confirmed); keep the text-based guesses as a
        // fallback in case a different screen/build phrases this differently.
        private static Button FindSkipLiftoffProButton()
        {
            Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
            foreach (Button btn in buttons)
            {
                if (btn == null || !btn.gameObject.activeInHierarchy || !btn.interactable) continue;
                string name = btn.name ?? "";
                string txt = GetButtonText(btn);
                bool isSkipByName = name.IndexOf("SignInAnonymous", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                     name.IndexOf("Anonymous", StringComparison.OrdinalIgnoreCase) >= 0;
                bool isSkipByText = !string.IsNullOrEmpty(txt) && (
                                     txt.IndexOf("skip", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                     txt.IndexOf("guest", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                     txt.IndexOf("anonymous", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                     txt.IndexOf("without", StringComparison.OrdinalIgnoreCase) >= 0);
                if (isSkipByName || isSkipByText)
                {
                    return btn;
                }
            }
            return null;
        }

        private static void HandleMainMenu()
        {
            // Gate (plugin-mode-split.md): no menu automation / auto sign-in in client mode. The
            // RunTick client branch already prevents reaching this; this guard is defense-in-depth
            // so the method is safe against any future caller.
            if (IsClientMode) return;

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
            bool hasDistinctCredentials = !string.IsNullOrEmpty(liftoffProUsername) && !string.IsNullOrEmpty(liftoffProPassword);
            if (!liftoffProLoginAttempted && (!useLiftoffPro || hasDistinctCredentials))
            {
                // use_liftoff_pro.txt=false, or distinct liftoff_pro_username/password.txt configured:
                // never click the MainMenu Pro sign-in button, since that would auto-login using
                // whatever account is already saved to this shared install's Credentials.xml
                // (production's own account) rather than the account we actually want this
                // instance to use. Falls through to MultiplayerMenu's sign-in screen instead,
                // where the credentialed or anonymous path takes over.
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Skipping default Liftoff Pro sign-in on MainMenu (useLiftoffPro=false or distinct credentials configured).");
                liftoffProLoginAttempted = true;
            }
            else if (!liftoffProLoginAttempted)
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

        // Fills the same MultiplayerSignIn form fieldUsername/fieldPassword (real names, confirmed
        // live 2026-07-02 via LogMultiplayerMenuState's button/input dump) that a human would type
        // into, then clicks buttonSignInCredentials — same UI path as SignInWithProAccount's
        // manual-credentials branch (OnSignInWithCredentials → username+password, not the saved
        // userid+authToken branch, since typing into fieldPassword resets useSavedCredentials via
        // the game's own OnPasswordChange listener).
        private static void HandleDistinctLiftoffProCredentials()
        {
            double timeSinceLoad = (DateTime.Now - sceneLoadTime).TotalSeconds;
            if (timeSinceLoad < 15.0)
            {
                if (DateTime.Now.Second % 5 == 0)
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Distinct Liftoff Pro credentials configured, waiting for sign-in UI to settle ({timeSinceLoad:F1}s)...");
                return;
            }

            InputField userField = FindInputFieldByName(new[] { "fieldUsername" }, "username");
            InputField passField = FindInputFieldByName(new[] { "fieldPassword" }, "password");
            if (userField == null || passField == null)
            {
                if (DateTime.Now.Second % 5 == 0)
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Waiting for Liftoff Pro username/password fields to appear...");
                return;
            }

            if (userField.text != liftoffProUsername) userField.text = liftoffProUsername;
            if (passField.text != liftoffProPassword) passField.text = liftoffProPassword;

            // Give the field writes above a tick to propagate (onValueChanged listeners, e.g. the
            // useSavedCredentials reset) before trusting the fields are actually in the state we
            // just set — same one-tick-behind caution used for the room-name InputField elsewhere.
            if (userField.text != liftoffProUsername || passField.text != liftoffProPassword) return;

            Button signInBtn = FindButtonByTextOrName("SIGN IN", new[] { "buttonSignInCredentials", "btnSignInCredentials" });
            if (signInBtn == null || !signInBtn.gameObject.activeInHierarchy || !signInBtn.interactable) return;

            // Same 30s cooldown as the other sign-in paths, for the same "still pending" reason.
            if ((DateTime.Now - lastCredentialSubmitTime).TotalSeconds > 30.0)
            {
                lastCredentialSubmitTime = DateTime.Now;
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Submitting distinct Liftoff Pro credentials for user '{liftoffProUsername}'.");
                signInBtn.onClick.Invoke();
            }
        }

        private static void HandleMultiplayerMenu()
        {
            // Gate (plugin-mode-split.md): no auto room creation, join-by-name, or sign-in
            // automation in client mode — the player drives their own menus. The RunTick client
            // branch already prevents reaching this; this guard is defense-in-depth.
            if (IsClientMode) return;

            // Known long-standing Liftoff quirk, confirmed live 2026-07-02 (affects both Pro
            // and anonymous sign-in): once "An authentication request is still pending. Cannot
            // connect." fires, every further click on this same MultiplayerMenu instance keeps
            // failing the same way — some auth-manager flag never clears itself. Confirmed via
            // reflection that Photon's own connection is healthy when this fires
            // (NetworkClientState=ConnectedToMasterServer, IsConnectedAndReady=true), so it's a
            // game-logic-level guard flag, not a Photon-level stuck connection.
            //
            // Tried and disproven: reloading only the MultiplayerMenu scene in place
            // (SceneManager.LoadScene("MultiplayerMenu")) does NOT clear it — looped 25+ times
            // live, every retry hit the identical error. That rules out scene-bound state and
            // confirms the stuck flag lives on a cross-scene-persistent object (SignInManager is
            // a LugusSingletonCrossScene<T>; PlatformProvider.Instance is a similar singleton),
            // which a same-scene reload never touches. Only the full MainMenu round-trip has
            // actually been confirmed to clear it.
            if (authPendingErrorDetected)
            {
                authPendingErrorDetected = false;
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Detected stuck 'authentication request still pending' error — cycling back to MainMenu to clear it (known Liftoff quirk).");
                liftoffProLoginAttempted = false;
                liftoffProLoginClickTime = DateTime.MinValue;
                lastSkipClickTime = DateTime.MinValue;
                lastSignInClickTime = DateTime.MinValue;
                lastCredentialSubmitTime = DateTime.MinValue;
                signInWasVisible = false;
                signInClickAttempted = false;
                NavigateToMainMenu();
                return;
            }

            DumpActiveSceneObjects();
            LogMultiplayerMenuState();

            // Distinct Liftoff Pro credentials (liftoff_pro_username.txt/liftoff_pro_password.txt)
            // take priority over both the anonymous and default-credentialed paths below — this
            // is how a test-client instance gets a genuinely distinct Photon identity instead of
            // colliding with other instances sharing this Steam login (see field comment above).
            bool hasDistinctCredentials = !string.IsNullOrEmpty(liftoffProUsername) && !string.IsNullOrEmpty(liftoffProPassword);
            if (hasDistinctCredentials)
            {
                HandleDistinctLiftoffProCredentials();
                return;
            }

            // use_liftoff_pro.txt=false: click through Skip/Guest/Anonymous instead of the
            // credentialed sign-in flow below. Checked first so it takes priority whenever
            // present — a useLiftoffPro=false instance should never fall into the sign-in
            // candidate picker further down.
            if (!useLiftoffPro)
            {
                Button skipBtn = FindSkipLiftoffProButton();
                if (skipBtn != null)
                {
                    // 15s, not the original 5s: the game appears to fire its own automatic
                    // connection/auto-login attempt as soon as this screen loads (matches the
                    // "waits 10s for auto-login first" behavior already documented for the
                    // credentialed path), and clicking Connect while that's still in flight is
                    // the likely cause of the "still pending" error seen live 2026-07-02 on
                    // literally the first click, even on a freshly-restarted Steam client.
                    double timeSinceLoadSkip = (DateTime.Now - sceneLoadTime).TotalSeconds;
                    if (timeSinceLoadSkip < 15.0)
                    {
                        if (DateTime.Now.Second % 5 == 0)
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Skip/anonymous button detected, waiting for UI to settle ({timeSinceLoadSkip:F1}s)...");
                        return;
                    }
                    // Confirmed live 2026-07-02: re-clicking Connect while the first anonymous
                    // auth request is still in flight gets rejected by the game with
                    // "An authentication request is still pending. Cannot connect." — 10s
                    // wasn't enough. Matches the 30s cooldown the credentialed-recovery path
                    // below already uses for the same reason ("auth takes time to process
                    // server-side").
                    if ((DateTime.Now - lastSkipClickTime).TotalSeconds > 30.0)
                    {
                        lastSkipClickTime = DateTime.Now;
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] useLiftoffPro=false — clicking skip/anonymous button: name='{skipBtn.name}' text='{GetButtonText(skipBtn)}'");
                        skipBtn.onClick.Invoke();
                    }
                    return;
                }
            }

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
    }
}
