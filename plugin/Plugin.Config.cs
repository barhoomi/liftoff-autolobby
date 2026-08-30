using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Reflection;
using System.Collections.Generic;
using System.Linq;
using BepInEx;
using BepInEx.Configuration;
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
    // Plugin role (server|client) + the single settings-source abstraction, plus the
    //     file-protocol config readers: admin ids, Liftoff Pro toggle/nickname/credentials,
    //     client script, rotation/keep-alive intervals, auto-start, shuffle mode, override
    //     game mode, playlist existence/listing. Mirrors the plugin<->orchestrator text-file
    //     protocol described in AGENTS.md.
    public partial class AutoLobbyPlugin
    {
        // ─────────────────────────────────────────────────────────────────────────────
        // Plugin role + settings source (plugin-mode-split.md, R3 of public-release-v1)
        //
        // One compiled DLL, two roles. `role = server` (default) is exactly today's
        // orchestrator-driven bot; `role = client` runs inside a real player's own game and
        // must never automate menus, auto-login, create rooms, rename the player, or quit the
        // game (see the gate table in the feature doc).
        //
        // Settings come from exactly ONE source per role — never a merge or a sync step
        // (AGENTS.md rule 4). Server reads the orchestrator's plain-text protocol files in
        // BepInEx/plugins/; client reads its own BepInEx ConfigFile (player-editable, visible
        // in ConfigurationManager). The role picks exactly one ISettingsSource; shared code
        // reads settings only through it.
        // ─────────────────────────────────────────────────────────────────────────────

        internal enum PluginRole { Server, Client }
        private static PluginRole pluginRole = PluginRole.Server;
        private static bool IsServerMode => pluginRole == PluginRole.Server;
        private static bool IsClientMode => pluginRole == PluginRole.Client;

        // Defaults to the file source so any code path that runs before InitializeRoleAndSettings
        // (there should be none) still behaves like today's server bot.
        private static ISettingsSource settingsSource = new FileSettingsSource();
        private static ISettingsSource Settings => settingsSource;

        // Local player identity, for client-mode admin resolution. localSteamId is captured from
        // the Steam init block in RunTick; the Photon UserId is resolved lazily (see Photon.cs).
        private static string localSteamId = "";

        // Resolved once, from Awake, using the plugin's BepInEx ConfigFile. Defaults to `server`
        // so an install with no config file present behaves exactly like today's bot.
        private static void InitializeRoleAndSettings(ConfigFile config)
        {
            var roleEntry = config.Bind(
                "General", "Role", "server",
                "Plugin role. 'server' = the dedicated auto-lobby bot (default; orchestrator-driven; " +
                "behaviorally unchanged). 'client' = runs inside a real player's own Liftoff: no menu " +
                "automation, no auto-login, no room creation, no nickname override, never quits the " +
                "game. Sits idle until invoked in a room the player hosts.");

            string roleStr = (roleEntry.Value ?? "server").Trim();
            pluginRole = roleStr.Equals("client", StringComparison.OrdinalIgnoreCase)
                ? PluginRole.Client
                : PluginRole.Server;

            settingsSource = IsClientMode
                ? (ISettingsSource)new ConfigSettingsSource(config)
                : new FileSettingsSource();

            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Role resolved to: {pluginRole} (config 'General.Role' = '{roleStr}'). " +
                                  $"Settings source: {settingsSource.GetType().Name}.");
        }

        // The config settings read from SHARED rotation/game-room paths, so they must resolve
        // in both roles once client rotation is engaged (client-lifecycle-commands.md, R4).
        // Server-automation-only settings (Liftoff Pro toggle/nickname/credentials) stay
        // file-only and are loaded only in server mode — client never touches those paths.
        //
        // RotationEngaged/RotationPaused (client-lifecycle-commands.md, R4 — extended mid-flight
        // by an operator scope amendment 2026-07-23 to be functional in BOTH roles, not
        // client-only): the /start /stop /pause /resume lifecycle-command state, one value per
        // role via this same settings-source abstraction — never a second mechanism. Defaults
        // differ deliberately per role (see each implementation): server defaults to engaged/
        // unpaused so a lobby nobody touches these commands in behaves exactly like before
        // R4 (server-mode-unchanged guarantee); client defaults to NOT engaged (boots idle,
        // per the original client-lifecycle-commands.md spec).
        private interface ISettingsSource
        {
            double RotationIntervalSeconds { get; }
            double KeepAliveSeconds { get; }
            bool AutoStart { get; }
            bool ShuffleMode { get; }
            bool DemocracyMode { get; }
            string OverrideGameMode { get; }   // null when unset (matches the file default)
            bool RotationEngaged { get; }       // default: server=true, client=false — see impls
            bool RotationPaused { get; }        // default: false in both roles
            void SetRotationEngaged(bool engaged);
            void SetRotationPaused(bool paused);

            // client-chat-presentation.md: customizable chat message templates. Client-only in
            // v1 (public-release-v1 freeze) -- FileSettingsSource returns its literal default
            // with NO file backing it (the server source gets no template files, by design; see
            // the feature doc's "Non-goals"), so server call sites that never read these are
            // unaffected either way, and any call site that DID read them in server mode would
            // still get today's exact wording. Every server-mode Command.Execute() call site
            // keeps using its original hardcoded string directly instead of reading these, so
            // server output stays byte-identical regardless. Placeholders are substituted by
            // Plugin.Chat.cs's RenderClientTemplate ({track}/{environment}/{time}, plus a couple
            // of command-specific extras documented per template below and in the feature doc).
            string SkipMessageTemplate { get; }     // /skip (admin path). {track} {environment}
            string TrackJumpTemplate { get; }       // /track. {track} {environment}
            string PrevTrackTemplate { get; }       // /prev. {track} {environment}
            string RotationStartedTemplate { get; } // /start (client-only wording). {track} {environment}
            string ExtendTemplate { get; }          // /extend (client-only wording). {seconds} {time} {track} {environment}
            string UpNextTemplate { get; }          // "Up next" rotation announcement. {track} {environment}

            // pre-rotation-callouts.md: the tiered pre-rotation warnings (5m/2m/1m by default),
            // in addition to the fixed final 10s warning above (UpNextTemplate/chatWarnedAboutNextRace,
            // untouched by this feature). CalloutOffsetsMinutes is a comma-separated list of
            // minutes-before-rotation; offsets at or beyond the current rotation interval are
            // skipped cleanly (see AnnounceCalloutTiersIfDue). Both members are literal defaults
            // (not file-backed) on FileSettingsSource -- server-mode customization is out of scope
            // for v1, same non-goal as every other *Template entry above.
            string CalloutOffsetsMinutes { get; }   // e.g. "5,2,1"
            string CalloutTemplate { get; }         // {time} {track} {environment}
        }

        // Server source: the orchestrator's plain-text protocol files in BepInEx/plugins/, read
        // fresh on each access so the orchestrator can change them live — identical to the
        // original Get* readers this replaced.
        private sealed class FileSettingsSource : ISettingsSource
        {
            public double RotationIntervalSeconds
            {
                get
                {
                    try
                    {
                        string p = Path.Combine(pluginPath, "rotation_interval.txt");
                        double v;
                        if (File.Exists(p) && double.TryParse(File.ReadAllText(p).Trim(), out v)) return v;
                    }
                    catch { }
                    return 600.0; // Default: 10 mins
                }
            }

            public double KeepAliveSeconds
            {
                get
                {
                    try
                    {
                        string p = Path.Combine(pluginPath, "keep_alive_seconds.txt");
                        double v;
                        if (File.Exists(p) && double.TryParse(File.ReadAllText(p).Trim(), out v)) return v;
                    }
                    catch { }
                    return 240.0; // Default: 4 minutes (resets timer right before 5m kick)
                }
            }

            public bool AutoStart => FileFlag("auto_start.txt");
            public bool ShuffleMode => FileFlag("shuffle_mode.txt");
            public bool DemocracyMode => FileFlag("democracy_mode.txt");

            public string OverrideGameMode
            {
                get
                {
                    try
                    {
                        string p = Path.Combine(pluginPath, "override_game_mode.txt");
                        if (File.Exists(p))
                        {
                            string mode = File.ReadAllText(p).Trim();
                            if (!string.IsNullOrEmpty(mode)) return mode;
                        }
                    }
                    catch { }
                    return null;
                }
            }

            // Distinct file names from auto_start.txt on purpose (client-lifecycle-commands.md,
            // "naming collision to handle") — auto_start.txt means "auto-click Start Race" and is
            // unrelated to the rotation lifecycle. Absent rotation_engaged.txt = true: an operator
            // who never runs /start or /stop and has no orchestrator writing this file gets the
            // exact pre-R4 server behavior (always-on rotation). Absent rotation_paused.txt = false.
            public bool RotationEngaged
            {
                get
                {
                    try
                    {
                        string p = Path.Combine(pluginPath, "rotation_engaged.txt");
                        if (File.Exists(p)) return !File.ReadAllText(p).Trim().Equals("false", StringComparison.OrdinalIgnoreCase);
                    }
                    catch { }
                    return true; // absent = engaged (server default: behaviorally unchanged)
                }
            }

            public bool RotationPaused => FileFlag("rotation_paused.txt");

            public void SetRotationEngaged(bool engaged) => WriteFileFlag("rotation_engaged.txt", engaged);
            public void SetRotationPaused(bool paused) => WriteFileFlag("rotation_paused.txt", paused);

            // client-chat-presentation.md: server mode gets NO template files, ever -- these are
            // literal in-memory defaults, never read from disk. No server call site actually
            // reads them today (each keeps its own hardcoded string), but the property exists so
            // FileSettingsSource fully implements ISettingsSource, matching every other member's
            // pattern in this class.
            public string SkipMessageTemplate => "Skipping to next track.";
            public string TrackJumpTemplate => "Rotating to track: {track} ({environment})";
            public string PrevTrackTemplate => "Rotating to the previous track: {track} ({environment})";
            public string RotationStartedTemplate => "Rotation started. Next: {environment} - {track}";
            public string ExtendTemplate => "Extended by {seconds}s. Next rotation in {time}s.";
            public string UpNextTemplate => "Up next: {environment} - {track}";

            // pre-rotation-callouts.md: literal defaults, never file-backed (server-mode
            // customization is out of scope for v1 -- see the ISettingsSource comment above).
            // No server call site reads CalloutTemplate (server hardcodes its own wording,
            // matching the UpNextTemplate precedent); CalloutOffsetsMinutes IS read by shared
            // code (AnnounceCalloutTiersIfDue) so the server bot gets the same 5m/2m/1m default
            // tiers as client mode, just not configurable without a code change.
            public string CalloutOffsetsMinutes => "5,2,1";
            public string CalloutTemplate => "Up next in {time}: {environment} - {track}";

            private static bool FileFlag(string fileName)
            {
                try
                {
                    string p = Path.Combine(pluginPath, fileName);
                    if (File.Exists(p)) return File.ReadAllText(p).Trim().Equals("true", StringComparison.OrdinalIgnoreCase);
                }
                catch { }
                return false;
            }

            private static void WriteFileFlag(string fileName, bool value)
            {
                try
                {
                    File.WriteAllText(Path.Combine(pluginPath, fileName), value ? "true" : "false");
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write {fileName}: {ex.Message}");
                }
            }
        }

        // Client source: the plugin's own BepInEx ConfigFile — player-editable, visible in
        // ConfigurationManager. Defaults mirror the server file defaults so nothing behaves
        // surprisingly.
        //
        // NOTE (R4 boundary): chat commands that CHANGE these settings still write the server
        // text files today (e.g. ShuffleCommand -> shuffle_mode.txt). Routing those WRITES to
        // ConfigEntry.Value for client mode is client-lifecycle-commands.md's job. This is inert
        // at R3 because client rotation is never engaged, so the only client-mode read of these
        // is the Awake-time initialisation of the shuffle/democracy statics (which correctly
        // returns these defaults) — there is no live path that could observe a stale value.
        private sealed class ConfigSettingsSource : ISettingsSource
        {
            private readonly ConfigEntry<double> rotationInterval;
            private readonly ConfigEntry<double> keepAlive;
            private readonly ConfigEntry<bool> autoStart;
            private readonly ConfigEntry<bool> shuffle;
            private readonly ConfigEntry<bool> democracy;
            private readonly ConfigEntry<string> overrideGameMode;
            private readonly ConfigEntry<bool> rotationEngaged;
            private readonly ConfigEntry<bool> rotationPaused;
            private readonly ConfigEntry<string> skipMessageTemplate;
            private readonly ConfigEntry<string> trackJumpTemplate;
            private readonly ConfigEntry<string> prevTrackTemplate;
            private readonly ConfigEntry<string> rotationStartedTemplate;
            private readonly ConfigEntry<string> extendTemplate;
            private readonly ConfigEntry<string> upNextTemplate;
            private readonly ConfigEntry<string> calloutOffsetsMinutes;
            private readonly ConfigEntry<string> calloutTemplate;

            public ConfigSettingsSource(ConfigFile config)
            {
                rotationInterval = config.Bind("Rotation", "IntervalSeconds", 600.0,
                    "Seconds each track stays in rotation before advancing.");
                keepAlive = config.Bind("Rotation", "KeepAliveSeconds", 240.0,
                    "Seconds of room inactivity before the plugin posts a keep-alive chat line.");
                autoStart = config.Bind("Rotation", "AutoStart", false,
                    "Automatically click START a few seconds after players are in the room.");
                shuffle = config.Bind("Rotation", "Shuffle", false,
                    "Shuffle the rotation order each full pass instead of playing it in file order.");
                democracy = config.Bind("Commands", "Democracy", false,
                    "Let players vote to skip a track with /skip instead of it being admin-only.");
                overrideGameMode = config.Bind("Rotation", "OverrideGameMode", "",
                    "Force a single game mode for every track (blank = use each rotation line's own mode).");
                // Client boots idle by design (client-lifecycle-commands.md): false until the
                // host runs /start. Distinct from AutoStart above, which is the Start-Race
                // auto-click, not the rotation lifecycle.
                rotationEngaged = config.Bind("Rotation", "Engaged", false,
                    "Whether the rotation manager is active in this room. Toggled by /start and /stop " +
                    "(client mode boots with this false and does nothing until /start).");
                rotationPaused = config.Bind("Rotation", "Paused", false,
                    "Whether the rotation timer is temporarily frozen. Toggled by /pause and /resume.");
                skipMessageTemplate = config.Bind("Messages", "SkipMessage", "Skipping to next track.",
                    "Chat message when skipping the current track. Placeholders: {track}, {environment}");
                trackJumpTemplate = config.Bind("Messages", "TrackJump", "Rotating to track: {track} ({environment})",
                    "Chat message when rotating to a specific track. Placeholders: {track}, {environment}");
                prevTrackTemplate = config.Bind("Messages", "PrevTrack", "Rotating to the previous track: {track} ({environment})",
                    "Chat message when rotating to the previous track. Placeholders: {track}, {environment}");
                rotationStartedTemplate = config.Bind("Messages", "RotationStarted", "Rotation started. Next: {environment} - {track}",
                    "Chat message when rotation starts (client-only wording). Placeholders: {track}, {environment}");
                extendTemplate = config.Bind("Messages", "Extend", "Extended by {seconds}s. Next rotation in {time}s.",
                    "Chat message when rotation is extended. Placeholders: {seconds}, {time}, {track}, {environment}");
                upNextTemplate = config.Bind("Messages", "UpNext", "Up next: {environment} - {track}",
                    "Chat message announcing the next track in rotation. Placeholders: {track}, {environment}");
                // pre-rotation-callouts.md: tiered pre-rotation warnings, in their own [Callouts]
                // section per the feature doc's suggested shape (distinct from the single-shot
                // [Messages] templates above, since this one pairs a message with a schedule).
                calloutOffsetsMinutes = config.Bind("Callouts", "OffsetsMinutes", "5,2,1",
                    "Comma-separated minutes-before-rotation for pre-rotation warnings, in addition " +
                    "to the fixed 10-second final warning. Example: '5,2,1'. An offset at or beyond " +
                    "the current rotation interval is skipped automatically (no negative-time spam).");
                calloutTemplate = config.Bind("Callouts", "Message", "Up next in {time}: {environment} - {track}",
                    "Chat message for the tiered pre-rotation warnings above. Placeholders: {time} " +
                    "(e.g. '5m', '90s'), {track}, {environment}");
            }

            public double RotationIntervalSeconds => rotationInterval.Value;
            public double KeepAliveSeconds => keepAlive.Value;
            public bool AutoStart => autoStart.Value;
            public bool ShuffleMode => shuffle.Value;
            public bool DemocracyMode => democracy.Value;
            public string OverrideGameMode => string.IsNullOrEmpty(overrideGameMode.Value) ? null : overrideGameMode.Value;
            public bool RotationEngaged => rotationEngaged.Value;
            public bool RotationPaused => rotationPaused.Value;
            public void SetRotationEngaged(bool engaged) => rotationEngaged.Value = engaged;
            public void SetRotationPaused(bool paused) => rotationPaused.Value = paused;
            public string SkipMessageTemplate => skipMessageTemplate.Value;
            public string TrackJumpTemplate => trackJumpTemplate.Value;
            public string PrevTrackTemplate => prevTrackTemplate.Value;
            public string RotationStartedTemplate => rotationStartedTemplate.Value;
            public string ExtendTemplate => extendTemplate.Value;
            public string UpNextTemplate => upNextTemplate.Value;
            public string CalloutOffsetsMinutes => calloutOffsetsMinutes.Value;
            public string CalloutTemplate => calloutTemplate.Value;
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

        // Server mode: admins come from admin_ids.txt (the operator's list). Client mode: the
        // installing player is implicitly the sole admin — there is no text file to hand-edit —
        // so "admin" means "this is the local player" (plugin-mode-split.md, "Client identity").
        private static bool IsAdmin(string userId)
        {
            if (IsClientMode) return IsLocalPlayer(userId);
            return adminIds.Contains(userId);
        }

        // True when the given Photon chat user id belongs to the local player. Matches primarily
        // on the local Photon UserId — the same value space as the incoming chat id in
        // ChatMessagePatch — with the local SteamID as a secondary check. Both resolve lazily and
        // are cached. NOTE (needs live verification): whether Liftoff's chat userId equals the
        // Photon LocalPlayer.UserId (and/or the SteamID string) must be confirmed in-game before
        // client-mode admin can be considered proven.
        private static bool IsLocalPlayer(string userId)
        {
            if (string.IsNullOrEmpty(userId)) return false;
            string photonId = GetLocalPhotonUserId();
            if (!string.IsNullOrEmpty(photonId) && string.Equals(userId, photonId, StringComparison.OrdinalIgnoreCase)) return true;
            if (!string.IsNullOrEmpty(localSteamId) && string.Equals(userId, localSteamId, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static void LoadUseLiftoffPro()
        {
            useLiftoffPro = true;
            string path = Path.Combine(pluginPath, "use_liftoff_pro.txt");
            if (File.Exists(path))
            {
                string content = File.ReadAllText(path).Trim();
                if (content.Equals("false", StringComparison.OrdinalIgnoreCase))
                {
                    useLiftoffPro = false;
                }
            }
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] useLiftoffPro = {useLiftoffPro}");
        }

        private static void LoadBotNickname()
        {
            botNickname = "";
            string path = Path.Combine(pluginPath, "bot_nickname.txt");
            if (File.Exists(path))
            {
                botNickname = File.ReadAllText(path).Trim();
            }
            if (!string.IsNullOrEmpty(botNickname))
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded bot nickname override: '{botNickname}'");
            }
        }

        private static void LoadLiftoffProCredentials()
        {
            liftoffProUsername = "";
            liftoffProPassword = "";
            string userPath = Path.Combine(pluginPath, "liftoff_pro_username.txt");
            string passPath = Path.Combine(pluginPath, "liftoff_pro_password.txt");
            if (File.Exists(userPath)) liftoffProUsername = File.ReadAllText(userPath).Trim();
            if (File.Exists(passPath)) liftoffProPassword = File.ReadAllText(passPath).Trim();
            if (!string.IsNullOrEmpty(liftoffProUsername) && !string.IsNullOrEmpty(liftoffProPassword))
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded distinct Liftoff Pro credentials for user '{liftoffProUsername}'.");
            }
        }

        // client_script.txt format: one step per line, "<delaySeconds> <message>" — delay is
        // seconds since entering the room (not since the previous step). Blank lines and lines
        // starting with '#' are skipped. Read once at Awake(), same as every other config file.
        private static void LoadClientScript()
        {
            clientScriptSteps.Clear();
            clientScriptNextIndex = 0;
            string path = Path.Combine(pluginPath, "client_script.txt");
            if (!File.Exists(path)) return;
            foreach (var line in File.ReadAllLines(path))
            {
                string trimmed = line.Trim();
                if (string.IsNullOrEmpty(trimmed) || trimmed.StartsWith("#")) continue;
                int spaceIdx = trimmed.IndexOf(' ');
                if (spaceIdx < 0) continue;
                double delay;
                if (double.TryParse(trimmed.Substring(0, spaceIdx), out delay))
                {
                    clientScriptSteps.Add(Tuple.Create(delay, trimmed.Substring(spaceIdx + 1)));
                }
            }
            if (clientScriptSteps.Count > 0)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded client script with {clientScriptSteps.Count} step(s).");
            }
        }

        // The following six readers now delegate to the active ISettingsSource (server = the
        // text files, unchanged; client = the BepInEx ConfigFile). Every call site keeps its
        // original name and semantics; only the source of the value moved behind the abstraction.
        private static double GetRotationInterval() => Settings.RotationIntervalSeconds;

        private static double GetKeepAliveInterval() => Settings.KeepAliveSeconds;

        private static bool GetAutoStart() => Settings.AutoStart;

        private static bool GetShuffleMode() => Settings.ShuffleMode;

        // democracy-skip.md: whether /skip is a public majority vote (true) or admin-only (false).
        private static bool GetDemocracyMode() => Settings.DemocracyMode;

        private static string GetOverrideGameMode() => Settings.OverrideGameMode;

        // client-lifecycle-commands.md: /start /stop /pause /resume state, one role-appropriate
        // source via Settings — see the ISettingsSource block above for the default rationale.
        private static bool IsRotationEngaged() => Settings.RotationEngaged;
        private static bool IsRotationPaused() => Settings.RotationPaused;

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
    }
}
