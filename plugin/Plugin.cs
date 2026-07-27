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
    // Version is stamped from the single source in Directory.Build.props by the
    // GeneratePluginVersion MSBuild target (plugin/LiftoffAutoLobby.csproj), which writes
    // PluginVersion.g.cs into obj/ before compilation. See also Commands/VersionCommand.cs.
    [BepInPlugin("com.barhoomi.liftoff.autolobby", "Liftoff Auto Lobby", PluginVersion.Number)]
    public partial class AutoLobbyPlugin : BaseUnityPlugin
    {
        private static string pluginPath;

        private static DateTime lastTickTime = DateTime.MinValue;
        private static DateTime lastActivityTime = DateTime.UtcNow;

        // Rotation & State Management
        private static DateTime roomCreatedTime = DateTime.MinValue;
        private static DateTime popupOpenedTime = DateTime.MinValue;
        private static DateTime popupSubmittedTime = DateTime.MinValue;
        private static DateTime lastStartGameClickedTime = DateTime.MinValue;
        private static bool popupWasOpen = false;
        private static bool isSubmittingSettings = false;
        private static string targetTrackName = "";
        private static string targetEnvironment = "";
        // The track/environment actually loaded in the room right now. Captured only at the moment
        // the settings popup submit succeeds (see CaptureLoadedTrack) — NOT read live from
        // targetTrackName, which during a rotation already points at the NEXT track before it has
        // loaded. Read by /info. Placeholders keep the first /info (before any rotation) non-blank.
        private static string currentTrackName = "starting up";
        private static string currentEnvironment = "";
        private static string targetGameMode = "";
        private static string targetLobbyName = "";
        private static bool isLeaving = false;

        private static bool chatWarnedAboutNextRace = false;
        private static bool liftoffProLoginAttempted = false;
        private static DateTime liftoffProLoginClickTime = DateTime.MinValue;
        private static DateTime lastSignInClickTime = DateTime.MinValue;
        // Multi-instance testing support: use_liftoff_pro.txt/bot_nickname.txt let separate
        // Liftoff processes (same Linux user, per docs/features/doing/multi-lobby-bot-scaling.md)
        // run as distinct anonymous "client" bots instead of all fighting over one Pro account.
        private static bool useLiftoffPro = true;
        private static string botNickname = "";
        private static bool nicknameApplied = false;
        private static DateTime lastSkipClickTime = DateTime.MinValue;
        // Distinct Liftoff Pro account per instance: anonymous sign-in still authenticates to
        // Photon via the shared Steam ticket (see multi-lobby-bot-scaling.md), so every
        // anonymous instance under one Steam login collides as the same Photon player. A
        // credentialed sign-in with its own account/password doesn't touch the Steam ticket at
        // all (confirmed via decompile: SignInWithProAccount never calls
        // PlatformProvider.GetPlatformAuthenticationData()), so distinct accounts should give
        // each instance a genuinely distinct identity.
        private static string liftoffProUsername = "";
        private static string liftoffProPassword = "";
        private static DateTime lastCredentialSubmitTime = DateTime.MinValue;
        // Scripted "client" mode for the black-box scenario harness (automated-testing.md
        // Phase 3): client_script.txt, when present, is a sequence of chat lines to send
        // automatically once in-room, so a test instance can trigger server-bot behavior
        // without GUI/keyboard automation. Absent file = zero effect on normal operation.
        private static List<Tuple<double, string>> clientScriptSteps = new List<Tuple<double, string>>();
        private static int clientScriptNextIndex = 0;
        // Set by OnUnityLogMessageReceived the instant the stuck-auth error fires; consumed
        // (and reset) at the top of the next HandleMultiplayerMenu() tick.
        private static bool authPendingErrorDetected = false;
        private static bool signInClickAttempted = false; // limits MultiplayerMenu sign-in to exactly one click per appearance (reduce-login-retry-attempts)
        private static bool signInWasVisible = false;      // tracks appearance transitions so signInClickAttempted resets per-appearance, not per-tick
        private static bool triedCustomContentTab = false;
        private static bool steamStatusLogged = false;
        private static string lastSceneName = "";
        private static DateTime sceneLoadTime = DateTime.MinValue;
        private static DateTime lastInRoomTime = DateTime.MinValue;
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

        // Democracy mode (democracy-skip.md): when enabled, /skip becomes a public majority
        // vote instead of admin-only. skipVotes holds the unique Photon User IDs of players
        // who have voted to skip the current track; cleared on new track load, scene change,
        // and room create/enter (see CaptureLoadedTrack, the scene-change block in
        // OnWillRenderCanvases/Update, and PhotonContainerPrefix's OnCreatedRoom/OnJoinedRoom).
        private static bool democracyEnabled = false;
        private static HashSet<string> skipVotes = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private static System.Random rng = new System.Random();
        private static bool maintenanceActive = false;
        private static DateTime maintenanceTime = DateTime.MaxValue;
        private static int lastMaintenanceWarningMinutes = -1;
        private static bool maintenanceWarning30sSent = false;
        private static bool maintenanceWarning10sSent = false;

        // Client-mode failure containment (plugin-mode-split.md, R3).
        // clientHardDisabled: latched after repeated uncaught tick failures in client mode, so a
        //   broken plugin degrades to "does nothing" instead of thrashing the player's game.
        private static bool clientHardDisabled = false;
        private static int consecutiveTickFailures = 0;
        private const int ClientTickFailureLimit = 15;

        // /start /stop /pause /resume (client-lifecycle-commands.md, R4). Engaged/paused state
        // lives behind ISettingsSource (IsRotationEngaged/IsRotationPaused, Plugin.Config.cs) —
        // no in-memory shadow copy. This is the only local state the feature needs: last-tick
        // marker for MaintainRotationPauseFreeze() below, which freezes roomCreatedTime without
        // editing Plugin.GameRoom.cs.
        private static DateTime rotationFreezeLastCheckTime = DateTime.MinValue;

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

        private void Awake()
        {
            Logger.LogInfo("[AutoLobbyPlugin] BepInEx Awake called!");
            try
            {
                pluginPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "BepInEx", "plugins");
                // Startup diagnostic (windows-compatibility.md R2): the single most load-bearing
                // path assumption in the plugin — surfaced explicitly so the Windows verification
                // pass can confirm it resolved to the right place.
                Logger.LogInfo($"[AutoLobbyPlugin] pluginPath resolved to: {pluginPath}");

                // Resolve role (server|client) and pick the single settings source BEFORE any
                // reader runs (plugin-mode-split.md). Config is the BaseUnityPlugin ConfigFile.
                InitializeRoleAndSettings(Config);

                LoadThemeConfig();

                // Server-automation-only config: admin list, Liftoff Pro sign-in toggle/nickname/
                // credentials, and the scenario-harness client script. Client mode never runs the
                // menu automation that reads these, and its admin is the local player — so it does
                // not touch these orchestrator files at all (one source per role, no merge).
                if (IsServerMode)
                {
                    LoadAdminIds();
                    LoadUseLiftoffPro();
                    LoadBotNickname();
                    LoadLiftoffProCredentials();
                    LoadClientScript();
                }
                else
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Client mode: admin = local player; skipping orchestrator config files (admin_ids/use_liftoff_pro/bot_nickname/credentials/client_script).");
                }

                // Load initial shuffle mode
                shuffleMode = GetShuffleMode();
                Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial shuffleMode: {shuffleMode}");

                // Load initial democracy mode (democracy-skip.md)
                democracyEnabled = GetDemocracyMode();
                Logger.LogInfo($"[AutoLobbyPlugin] Loaded initial democracyEnabled: {democracyEnabled}");

                // Register all chat commands with the command registry (replaces the old
                // hardcoded HandleChatCommand switch).
                CommandRegistry.Initialize();

                // Apply Harmony patches to fix database loading exceptions
                ApplyHarmonyPatches();

                // Dynamically resolve multiplayer client connection check method
                ResolveMultiplayerClientCheck();

                // Subscribe to the static Canvas render event (runs on main thread every frame)
                Canvas.willRenderCanvases += OnWillRenderCanvases;

                // Known long-standing Liftoff quirk (both Pro and anonymous sign-in): the
                // sign-in flow can get stuck reporting "An authentication request is still
                // pending. Cannot connect." on every further click, seemingly forever, until
                // the MultiplayerMenu scene is torn down and reloaded (previously worked
                // around by hand: back to MainMenu, back into Multiplayer). Hooking Unity's
                // log callback lets HandleMultiplayerMenu() detect this the moment it fires
                // and trigger that same recovery automatically instead of hanging.
                Application.logMessageReceived += OnUnityLogMessageReceived;

                Logger.LogInfo("[AutoLobbyPlugin] Static Canvas.willRenderCanvases hook registered successfully!");
            }
            catch (Exception ex)
            {
                Logger.LogError($"[AutoLobbyPlugin] Static hook registration failed: {ex.Message}");
            }
        }

        // Registered once in Awake() via Application.logMessageReceived. Runs on Unity's main
        // thread for synchronous Debug.LogError calls (which this is), so it's safe to touch
        // plugin state here, but kept minimal (just sets a flag) rather than acting directly —
        // HandleMultiplayerMenu() is where scene navigation / all other plugin state changes
        // normally happen, so recovery is handled there on the next tick instead of here.
        private static void OnUnityLogMessageReceived(string logString, string stackTrace, LogType type)
        {
            if (type != LogType.Error) return;
            if (!string.IsNullOrEmpty(logString) &&
                logString.IndexOf("authentication request is still pending", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                authPendingErrorDetected = true;
                // Temporary root-cause diagnostics (2026-07-02): captures Photon's own
                // connection state machine at the exact moment this error fires, before any
                // recovery runs, to check whether PhotonNetwork.Disconnect() alone could
                // resolve this without the full MainMenu round-trip. Remove once resolved.
                LogPhotonAuthDiagnostics();
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
                consecutiveTickFailures = 0; // reached on any normal completion (incl. early returns)
            }
            catch (Exception ex)
            {
                // Catch all to ensure we never crash Unity's canvas rendering loop
                NoteTickFailure(ex, "OnWillRenderCanvases");
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
                consecutiveTickFailures = 0; // reached on any normal completion (incl. early returns)
            }
            catch (Exception ex)
            {
                NoteTickFailure(ex, "Update");
            }
        }

        // Server-only maintenance tick: external-trigger detection + scheduled-shutdown warnings
        // and the final Application.Quit(). Extracted verbatim from RunTick so it can be gated to
        // server mode without touching its logic. Returns true when the game is quitting (the
        // caller must then stop the rest of the tick), matching the original `return`.
        private static bool RunServerMaintenanceTick()
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
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance scheduled in {FormatVariable($"3.0m")} (triggered externally).");
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance mode triggered externally.");
                    }
                }
                else
                {
                    if (maintenanceActive)
                    {
                        CancelMaintenance();
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Scheduled maintenance cancelled externally.");
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
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Going down for maintenance.");
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Maintenance time reached. Exiting game.");
                    Application.Quit();
                    return true; // Prevent running other tick logic
                }
                else
                {
                    int remainingMinutes = (int)Math.Ceiling(remainingSecs / 60.0);
                    if (remainingMinutes > 0 && remainingMinutes != lastMaintenanceWarningMinutes && remainingSecs > 30.0)
                    {
                        lastMaintenanceWarningMinutes = remainingMinutes;
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"{remainingMinutes}m")}.");
                    }
                    else if (remainingSecs <= 30.0 && !maintenanceWarning30sSent)
                    {
                        maintenanceWarning30sSent = true;
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"30s")}.");
                    }
                    else if (remainingSecs <= 10.0 && !maintenanceWarning10sSent)
                    {
                        maintenanceWarning10sSent = true;
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance in {FormatVariable($"10s")}.");
                    }
                }
            }
            return false;
        }

        // Keeps HandleGameRoom's rotation timer (Plugin.GameRoom.cs: elapsed = DateTime.Now -
        // roomCreatedTime, vs GetRotationInterval()) from reaching the interval while disengaged
        // (/stop) or paused (/pause) — WITHOUT editing Plugin.GameRoom.cs, which belongs to the
        // other agent working this plugin in parallel. Each tick this holds, nudges
        // roomCreatedTime forward by the wall-clock delta since the last check, pinning `elapsed`
        // at whatever it was when the freeze began. /skip and /track still work while frozen:
        // both set skipRequested = true, which HandleGameRoom's trigger ORs around unconditionally
        // — never gated by this freeze to begin with. /resume just stops calling this, so elapsed
        // resumes growing from the frozen value ("remaining time preserved"); /start instead
        // forces skipRequested = true itself for an immediate fresh track application. Skips the
        // two pre-existing sentinels of roomCreatedTime (MinValue = room not entered;
        // MaxValue = settings popup mid-submit) so this never collides with GameRoom/RoomSetup's
        // own use of them.
        private static void MaintainRotationPauseFreeze()
        {
            bool shouldFreeze = !IsRotationEngaged() || IsRotationPaused();
            if (!shouldFreeze || roomCreatedTime == DateTime.MinValue || roomCreatedTime == DateTime.MaxValue)
            {
                rotationFreezeLastCheckTime = DateTime.MinValue;
                return;
            }

            if (rotationFreezeLastCheckTime == DateTime.MinValue)
            {
                // First tick of this freeze window — nothing elapsed yet to compensate for.
                rotationFreezeLastCheckTime = DateTime.Now;
                return;
            }

            roomCreatedTime += (DateTime.Now - rotationFreezeLastCheckTime);
            rotationFreezeLastCheckTime = DateTime.Now;
        }

        private static void RunTick()
        {
            // Failure containment (plugin-mode-split.md): if client mode has hit repeated
            // uncaught tick errors it self-disables for the session rather than thrashing the
            // player's game. Server mode never sets this flag (it has a watchdog).
            if (clientHardDisabled) return;

            // client-lifecycle-commands.md (R4): hold the rotation timer if a server admin has
            // /stop'd or /pause'd rotation (HandleClientTick does the client-mode equivalent
            // below). No-op in the default engaged/unpaused state.
            if (IsServerMode) MaintainRotationPauseFreeze();

            ApplyBotNicknameIfNeeded();

            // Maintenance mode is server-only: a player's game must never be closed from under
            // them (DANGER gate). The whole block — external trigger, warnings, and the final
            // Application.Quit() — runs only in server mode.
            if (IsServerMode && RunServerMaintenanceTick()) return;

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
                            // Captured for client-mode admin resolution (IsLocalPlayer): in client
                            // mode the installing player — this Steam account — is implicitly admin.
                            if (steamId != 0) localSteamId = steamId.ToString();
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
                string previousSceneName = lastSceneName;
                lastSceneName = sceneName;
                sceneLoadTime = DateTime.Now;
                lastInRoomTime = DateTime.MinValue;
                sceneObjectsDumped = false;
                lastMenuStateDumpTime = DateTime.MinValue;
                // democracy-skip.md: any scene change invalidates in-flight skip votes.
                skipVotes.Clear();
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Scene changed to: {sceneName}");
                LogEvent("scene_change", ("scene", sceneName));
                // Structured JSON file event (A3): from/to per the canonical schema. The very
                // first transition has no meaningful prior scene, so "from" is omitted (null).
                LogJsonEvent("scene_change",
                    ("from", string.IsNullOrEmpty(previousSceneName) ? null : previousSceneName),
                    ("to", sceneName));

                // Reset room timer when loading into a flight level scene
                if (sceneName != "MainMenu" && sceneName != "MultiplayerMenu" &&
                    sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene")
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Level loaded. Resetting room timer.");
                    roomCreatedTime = DateTime.Now;
                    lastActivityTime = DateTime.UtcNow;
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

            // ── Client-mode gate (plugin-mode-split.md, R3) ────────────────────────────────
            // Everything below this point is autonomous UI automation that only ever runs in
            // server mode: the private-room rename recovery net, popup dismissal, the settings-
            // popup create/update flow (ConfigureAndCreateRoom), and the MainMenu / MultiplayerMenu
            // / FlightLevel scene handlers. Client mode must never automate menus, sign in, create
            // rooms, or exit a flight level the player is in — it sits idle in the player's game
            // until the player engages rotation via a lifecycle command (client-lifecycle-commands
            // .md, R4). The shared HandleGameRoom / HandleFlightLevel / settings-Update path is
            // driven from HandleClientTick once engaged. This single branch is what gates the whole
            // server-only tail; in server mode IsClientMode is always false, so it is inert.
            if (IsClientMode)
            {
                HandleClientTick(sceneName);
                return;
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
                QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room rename to '{FormatVariable($"{pendingPrivateRoomName}")}' got stuck and was aborted — recovering with a public room.");
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
                        lastActivityTime = DateTime.UtcNow;
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

        // Client-mode tick (plugin-mode-split.md R3; engaged rotation wired by
        // client-lifecycle-commands.md R4). Reached only via the IsClientMode branch in RunTick,
        // so no server automation (menus, sign-in, room creation, maintenance quit, nickname) can
        // run. Idle (IsRotationEngaged() false, the client default) until the host runs /start —
        // this must stay a true early-out. Once engaged it drives the same in-room settings-
        // Update path server rotation uses (ApplyRoomSettingsPopup(popup, allowCreate:false) —
        // never Create), only while the player is in that room's waiting-room UI. Never navigates
        // a menu, joins/creates a room, or clicks anything the player did not ask for.
        //
        // Self-contained re-implementation of RunTick's server-tail popup-handling block, not a
        // shared extraction: that block is unreachable from client mode by construction (this
        // branch returns before it), and sharing it would mean restructuring RunTick's pinned
        // body more invasively than the one-line MaintainRotationPauseFreeze hook above. This
        // duplication is between two branches of the same method family (Plugin.cs), not the
        // two-state-files anti-pattern AGENTS.md rule 4 warns about.
        private static void HandleClientTick(string sceneName)
        {
            if (!IsRotationEngaged()) return; // idle: R3/R4 default for client, or after /stop

            MaintainRotationPauseFreeze(); // hold the timer while paused; no-op once engaged+unpaused

            if (sceneName != "MultiplayerMenu")
            {
                // client-ingame-track-change.md (Plan B'): a flight level is no longer a dead end —
                // rotation keeps running while the host is mid-race and applies the next track in
                // place through the in-game settings popup. This still NEVER navigates, never opens
                // the player's pause menu, and never takes them out of the flight level. Every other
                // scene (main menu, splash/loading/unknown, or the player briefly backed out) stays
                // a true no-op exactly as before. Flight-level detection mirrors the server tail's
                // test in RunTick: "not one of the known non-flight scenes".
                if (!string.IsNullOrEmpty(sceneName) && sceneName != "MainMenu" &&
                    sceneName != "SplashScreen" && sceneName != "LoadingScreen" && sceneName != "UnknownScene")
                {
                    HandleClientInFlightRotation();
                }
                return;
            }

            GameObject gameRoomObj = GameObject.Find("GameRoom");
            bool inRoom = (gameRoomObj != null && gameRoomObj.activeInHierarchy);
            if (!inRoom) return; // no lobby-list/sign-in automation — that stays server-only.

            PopupQuickPlayMultiplayerSetup popup = GameObject.FindObjectOfType<PopupQuickPlayMultiplayerSetup>();
            bool popupOpen = (popup != null && popup.gameObject.activeInHierarchy);

            if (popupOpen)
            {
                if (!popupWasOpen)
                {
                    popupOpenedTime = DateTime.Now;
                    isSubmittingSettings = false;
                    triedCustomContentTab = false;
                    targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode);
                    popupWasOpen = true;
                }

                if (isSubmittingSettings) return; // waiting for the popup to close, same as server

                double popupAge = (DateTime.Now - popupOpenedTime).TotalSeconds;
                if (popupAge < 2.0) return; // let the popup finish initializing, same as server

                // Re-synced every tick (not just on open) so a transient Photon read failure on
                // the first attempt self-heals instead of leaving the player's room briefly
                // mis-configured — see the method's comment for what this protects against.
                SyncClientRoomIdentityForPopup();
                ApplyRoomSettingsPopup(popup, allowCreate: false);
                return;
            }

            if (popupWasOpen)
            {
                if (roomCreatedTime == DateTime.MaxValue)
                {
                    roomCreatedTime = DateTime.Now;
                    lastActivityTime = DateTime.UtcNow;
                    chatWarnedAboutNextRace = false;
                }
                isSubmittingSettings = false;
            }
            popupWasOpen = false;

            HandleGameRoom(); // shared: rotation timer, keep-alive, auto-start (auto-start still
                               // gated by GetAutoStart(), unrelated to the engaged/paused gate).
        }

        // client-lifecycle-commands.md (R4 follow-up decision): ApplyRoomSettingsPopup
        // (Plugin.RoomSetup.cs, out of this feature's file partition) re-applies room_private.txt
        // and targetLobbyName on every Update — correct for server (the orchestrator owns those
        // files) but harmful for a client: room_private.txt is normally absent, whose read-side
        // default is "private", so an unmodified Update would silently flip a player's public
        // room private and rename it on the first rotation. Fix: snapshot the room's ACTUAL
        // current visibility/name from Photon (TryGetRoomInfo, Plugin.Photon.cs) into the same
        // inputs ApplyRoomSettingsPopup reads, right before it runs, so applying settings changes
        // only track/environment/mode and is a deliberate no-op for the player's own room name/
        // privacy.
        private static void SyncClientRoomIdentityForPopup()
        {
            bool isVisible; string roomName; int maxPlayers, playerCount;
            if (!TryGetRoomInfo(out isVisible, out roomName, out maxPlayers, out playerCount)) return;
            if (!string.IsNullOrEmpty(roomName)) targetLobbyName = roomName;
            try
            {
                File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), isVisible ? "false" : "true");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Client: failed to sync room_private.txt from live room state: {ex.Message}");
            }
        }

        // Failure containment for client mode (plugin-mode-split.md). On a player's machine there
        // is no watchdog, so a plugin that throws every tick is worse than one that does nothing:
        // after repeated consecutive failures, latch off for the session and say so once. Server
        // mode is unaffected — it keeps ticking exactly as before (its watchdog handles recovery),
        // so this changes nothing for the server role.
        private static void NoteTickFailure(Exception ex, string where)
        {
            UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in {where}: {ex}");
            if (!IsClientMode) return;
            consecutiveTickFailures++;
            if (!clientHardDisabled && consecutiveTickFailures >= ClientTickFailureLimit)
            {
                clientHardDisabled = true;
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Client mode: {consecutiveTickFailures} consecutive tick failures — self-disabling for this session to protect the player's game.");
                try
                {
                    SendChatMessage($"{FormatTag("SYSTEM", activeTheme.systemTagColor)} The auto-lobby plugin hit repeated errors and has disabled itself for this session.");
                }
                catch { /* best-effort; never throw from the failure path */ }
            }
        }

        private static bool sceneObjectsDumped = false;
        private static DateTime lastMenuStateDumpTime = DateTime.MinValue;

        // Structured logging slice for the black-box scenario harness
        // (docs/features/doing/automated-testing.md Phase 2/3): a small set of JSON-line
        // events, additive alongside the existing free-text Debug.Log calls rather than
        // replacing them, so scenario assertions can grep/parse instead of pattern-matching
        // free text. Escapes more than JsonEscape above since these fields carry
        // user-supplied chat text, not just internal track names.
        private static string JsonEscapeStrict(string s) => s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", "\\n").Replace("\r", "").Replace("\t", "\\t");

        private static void LogEvent(string eventType, params (string key, string value)[] fields)
        {
            try
            {
                var sb = new StringBuilder();
                sb.Append("{\"event\":\"").Append(JsonEscapeStrict(eventType)).Append("\"");
                foreach (var f in fields)
                {
                    sb.Append(",\"").Append(f.key).Append("\":\"").Append(JsonEscapeStrict(f.value ?? "")).Append("\"");
                }
                sb.Append("}");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin:EVENT] {sb}");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] LogEvent failed: {ex.Message}");
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
    }
}
