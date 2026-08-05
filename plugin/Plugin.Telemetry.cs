using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using Photon.Realtime;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace LiftoffAutoLobby
{
    // MODE: shared
    // Telemetry foundation — Photon property-update observation, room-level race lifecycle
    // events, system-chat capture, and the runtime reachability probe.
    //
    // Spec of record: docs/features/doing/player-telemetry-spike.md (§"The central anchor",
    // §"Proposed JSONL events", §"Design notes", §"Reachability caveats"), folded into
    // docs/features/doing/lifecycle-event-logging.md as its "Phase 2 — telemetry foundation".
    //
    // Deliberately NOT in this file (belongs to backlog/player-stats-tracking.md, per the
    // spike's "Recommended implementation split"): parsing the per-player `GameModeState`
    // value, and therefore the `lap` / `race_result` / `player_crash` / `player_reset` / `pb`
    // events and the `race_end` `standings` array. What lives here is the plumbing those need
    // — the two callback branches, the de-dup/edge-detect snapshot cache, and a probe that
    // tells the next implementer exactly what those values look like at runtime.
    //
    // Everything below is inert-safe with no live data: every read is shape-based and
    // failure-tolerant, and when a shape is not found NOTHING is logged rather than a guess
    // (AGENTS.md rule 2 — a non-null MethodInfo/PropertyInfo is never treated as success).
    public partial class AutoLobbyPlugin
    {
        // ───────────────────────────────────────────────────────────────────────────────
        // Snapshot / de-dup cache (spike "Design notes": one handler, one state cache)
        //
        // Static, in-memory only: it dies with the game process, which is correct — a race
        // does too (AGENTS.md rule 5). It is deliberately NOT persisted to a state file:
        // a second derived file that must stay in sync with Photon's own room state is
        // exactly the class of bug AGENTS.md rule 4 exists to prevent.
        //
        // Phase 2 stores a generic *content signature* of the changed properties rather than
        // the semantic tuple the spike proposes ((result, flightState, lapCount,
        // lastCheckpointId, lastCheckpointLap)) — that tuple requires parsing GameModeState,
        // which is player-stats-tracking.md's scope. The signature is the same mechanism at a
        // coarser granularity: it answers "is this dispatch new information or a repeat?",
        // which is what the probe needs to report a real duplicate-dispatch rate. When the
        // stats work lands, it replaces the signature value with the semantic tuple; the
        // dictionary, its key, and its lifecycle stay as they are.
        // ───────────────────────────────────────────────────────────────────────────────
        private static readonly Dictionary<string, string> telemetryPlayerPropSignatures =
            new Dictionary<string, string>(StringComparer.Ordinal);

        // Last-seen signature of the room custom properties delivered to OnRoomPropertiesUpdate.
        private static string telemetryRoomPropSignature;

        // The Hashtable key the room's shared race state actually arrived under, LEARNED at
        // runtime from a value we positively shape-identified (never a hard-coded literal —
        // the key is written by obfuscated members and is not stable across game patches; see
        // GetRoomPropertiesSnapshot's comment in Plugin.Photon.cs for the same discipline).
        // Only used to recognise the *removal* of that property (Photon delivers a removal as
        // the key with a null value), which is one of the race_end triggers.
        private static string telemetrySharedRaceStateKey;

        // Open-race bookkeeping, all derived from observed room properties.
        private static bool telemetryRaceOpen;
        private static float telemetryRaceStartTimeValue;
        private static DateTime telemetryRaceObservedStart = DateTime.MinValue;
        private static int telemetryRaceRacers = -1;
        private static string telemetryRaceMode;

        // Scene the caches were last valid for. The spike requires clearing on scene change;
        // this is checked lazily at handler entry instead of from RunTick's scene-change block
        // in Plugin.cs, so this feature adds no shared-field/tick edits to that file (the one
        // file the per-file mutex in AGENTS.md still treats as whole-file).
        private static string telemetryCacheScene;

        // ───────────────────────────────────────────────────────────────────────────────
        // Reachability probe (spike §"Reachability caveats", questions 1-4)
        //
        // Gating decision (recorded in lifecycle-event-logging.md's Phase 2 section):
        // BOTH, in two tiers.
        //   Tier 1, always on, hard-capped: the first TelemetryProbeUnityLogCap property
        //     updates after every scene change / observed race start go to the Unity log only.
        //     Always-on matters because the plugin log is collected from every session with no
        //     operator action — a live race that nobody remembered to "arm" still answers the
        //     spike's four questions. The cap is what keeps checkpoint-rate spam out of a soak.
        //   Tier 2, opt-in, uncapped + JSONL: if the flag file telemetry_probe.txt exists in
        //     the BepInEx plugins dir, the cap is lifted and each observation also becomes a
        //     `telemetry_probe` JSONL event. Presence-only, no contents, matching the existing
        //     plugin<->orchestrator file protocol (identical convention to
        //     maintenance_active.txt, Plugin.cs RunServerMaintenanceTick). Polled at most
        //     every TelemetryProbeFlagCacheSeconds so a property update never costs a stat().
        // ───────────────────────────────────────────────────────────────────────────────
        private const int TelemetryProbeUnityLogCap = 40;
        private const string TelemetryProbeFlagFileName = "telemetry_probe.txt";
        private const double TelemetryProbeFlagCacheSeconds = 10.0;
        private const int TelemetryProbeDescriptionMaxChars = 700;

        private static int telemetryProbeUnityLines;
        private static bool telemetryProbeFlagValue;
        private static DateTime telemetryProbeFlagChecked = DateTime.MinValue;

        // ───────────────────────────────────────────────────────────────────────────────
        // Callback branches (called from PhotonContainerPrefix in Plugin.Harmony.cs)
        //
        // Signatures confirmed by decompile of PhotonRealtime.dll per AGENTS.md rule 1 —
        // Photon.Realtime.InRoomCallbacksContainer:
        //     public void OnPlayerPropertiesUpdate(Player targetPlayer, Hashtable changedProp)
        //     public void OnRoomPropertiesUpdate(Hashtable propertiesThatChanged)
        // Both are plain single-body `foreach` dispatchers over their own List<IInRoomCallbacks>
        // (same structure lifecycle-event-logging.md already established for the join/leave
        // callbacks), and ApplyHarmonyPatches already patches every `On*` method on every
        // container type — so no new Harmony target is needed for either.
        // ───────────────────────────────────────────────────────────────────────────────
        private static void HandlePlayerPropertiesUpdate(object playerObj, object changedProps)
        {
            try
            {
                ResetTelemetryCachesIfSceneChanged();

                string nick, userId;
                ReadPhotonPlayerInfo(playerObj, out nick, out userId);
                string cacheKey = TelemetryPlayerCacheKey(playerObj, nick, userId);

                string signature = BuildPropertiesSignature(changedProps);
                string previous;
                bool changed = !(telemetryPlayerPropSignatures.TryGetValue(cacheKey, out previous)
                                 && string.Equals(previous, signature, StringComparison.Ordinal));
                telemetryPlayerPropSignatures[cacheKey] = signature;

                // Phase 2 emits no per-player JSONL event here on purpose: turning these
                // updates into lap/crash/result events requires reading GameModeState, which
                // is player-stats-tracking.md's scope. The probe below is what tells that
                // work what it is actually receiving.
                LogTelemetryProbe("player", string.IsNullOrEmpty(nick) ? cacheKey : nick, userId, changedProps, changed);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] HandlePlayerPropertiesUpdate failed: {ex.Message}");
            }
        }

        private static void HandleRoomPropertiesUpdate(object changedProps)
        {
            try
            {
                ResetTelemetryCachesIfSceneChanged();

                string signature = BuildPropertiesSignature(changedProps);
                bool changed = !string.Equals(signature, telemetryRoomPropSignature, StringComparison.Ordinal);
                telemetryRoomPropSignature = signature;

                LogTelemetryProbe("room", null, null, changedProps, changed);
                InspectRoomPropsForRaceLifecycle(changedProps);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] HandleRoomPropertiesUpdate failed: {ex.Message}");
            }
        }

        // Stable per-player cache key. Photon's UserId is the identity every other event in
        // this log uses, so it is preferred; ActorNumber (a confirmed public property on
        // Photon.Realtime.Player: `public int ActorNumber => actorNumber;`) is the fallback for
        // the identity-less dispatches lifecycle-event-logging.md documented, and the nick is
        // the last resort. Never returns null, so the cache can never key on "".
        private static string TelemetryPlayerCacheKey(object playerObj, string nick, string userId)
        {
            if (!string.IsNullOrEmpty(userId)) return userId;
            try
            {
                if (playerObj != null)
                {
                    PropertyInfo actorProp = playerObj.GetType().GetProperty("ActorNumber",
                        BindingFlags.Public | BindingFlags.Instance);
                    if (actorProp != null && actorProp.PropertyType == typeof(int))
                        return "actor:" + ((int)actorProp.GetValue(playerObj, null)).ToString(CultureInfo.InvariantCulture);
                }
            }
            catch { /* fall through to the nick */ }
            return string.IsNullOrEmpty(nick) ? "unknown" : "nick:" + nick;
        }

        // Clears every cache when the active scene changed since the last observation
        // (spike "Design notes"). Cheap: one string compare per property update in the common
        // case. SceneManager.GetActiveScene() is main-thread-only, but Photon callbacks are
        // dispatched from PUN's MonoBehaviour update loop — the same thread that already runs
        // this plugin's tick and the existing LogJsonEvent file writes from these callbacks.
        private static void ResetTelemetryCachesIfSceneChanged()
        {
            try
            {
                string scene = SceneManager.GetActiveScene().name;
                if (string.Equals(scene, telemetryCacheScene, StringComparison.Ordinal)) return;
                telemetryCacheScene = scene;
                ClearTelemetryCaches($"scene change -> {scene}");
            }
            catch { /* if the scene can't be read, keep the existing caches rather than churn */ }
        }

        // Single clear point for every piece of telemetry state, so "what dies when" is one
        // decision in one place (AGENTS.md rule 5). Note the deliberate asymmetry: the OPEN
        // RACE is not cleared here, because a scene change into the flight level happens right
        // after a race starts — dropping the open race there would lose every race_end.
        private static void ClearTelemetryCaches(string reason)
        {
            telemetryPlayerPropSignatures.Clear();
            telemetryRoomPropSignature = null;
            telemetryProbeUnityLines = 0;
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Telemetry caches cleared ({reason}).");
        }

        // ───────────────────────────────────────────────────────────────────────────────
        // race_start / race_end (room-level only)
        //
        // Anchor, confirmed by decompile of Assembly-CSharp (Liftoff 1.7.4) per AGENTS.md
        // rule 1 — the race managers publish a shared game state into the ROOM custom
        // properties at race start:
        //     namespace Liftoff.Multiplayer.ClassicRace { [Serializable] public class
        //         SharedGameState { public float RaceStartTime {get;set;}
        //                           public int InitialRacersCount {get;set;} } }
        //     namespace Liftoff.Multiplayer.DropoutRace  { ...identical shape... }
        // and the Classic Race manager's start coroutine builds exactly
        //     new SharedGameState { RaceStartTime = <server time + 2s + max ping>,
        //                           InitialRacersCount = ParticipatingPlayers.Count() }
        // then publishes it and waits for the room properties to echo it back before firing
        // RPCStartCountdown. Both type names and both property names survived obfuscation,
        // which is why they are safe to key off; the Hashtable KEY they ride under does not,
        // so it is never named here (value-shape path (b) of the spike's §2).
        // ───────────────────────────────────────────────────────────────────────────────
        private static void InspectRoomPropsForRaceLifecycle(object changedProps)
        {
            IDictionary dict = changedProps as IDictionary;
            if (dict == null) return;

            foreach (DictionaryEntry entry in dict)
            {
                string key = entry.Key == null ? "" : entry.Key.ToString();

                if (entry.Value == null)
                {
                    // Photon delivers a REMOVED custom property as the key with a null value.
                    // Only meaningful for the key we positively identified as the shared race
                    // state at race start — anything else null is another system's property.
                    if (telemetryRaceOpen && telemetrySharedRaceStateKey != null &&
                        string.Equals(key, telemetrySharedRaceStateKey, StringComparison.Ordinal))
                    {
                        LogRaceEndEvent("shared_state_cleared");
                    }
                    continue;
                }

                float raceStartTime;
                int racers;
                string mode;
                if (!TryReadSharedRaceState(entry.Value, out raceStartTime, out racers, out mode)) continue;

                // Learned (never guessed) key, refreshed on every positive identification so a
                // game patch that renames the key self-heals on the next race.
                telemetrySharedRaceStateKey = key;

                if (telemetryRaceOpen && raceStartTime == telemetryRaceStartTimeValue)
                {
                    // Same race re-published (Photon re-broadcasts, duplicate dispatch) — not
                    // a new race. This is the room-level half of the de-dup the spike asks for.
                    continue;
                }

                if (telemetryRaceOpen)
                {
                    // A different race started while one was still open: the previous race
                    // necessarily ended, we just never saw the end signal. Emitting it here
                    // (with an explicit cause) is strictly better than silently dropping it —
                    // the cause field tells the reader the timing is "no later than", not exact.
                    LogRaceEndEvent("race_superseded");
                }

                telemetryRaceOpen = true;
                telemetryRaceStartTimeValue = raceStartTime;
                telemetryRaceObservedStart = DateTime.Now;
                telemetryRaceRacers = racers;
                telemetryRaceMode = mode;

                // Spike "Design notes": clear the per-player cache on an observed race start so
                // each race diffs from a clean baseline (and the probe budget resets with it).
                ClearTelemetryCaches("race start observed");

                LogRaceStartEvent();
            }
        }

        // Shape-based read of a room-property value as a race shared game state. Returns false
        // — and therefore logs nothing at all — for anything that is not positively identified,
        // including the case where Photon hands us the value still serialized as a byte[]
        // because the game's custom-type registration is scene-gated (spike reachability
        // question 3). "Log nothing rather than guess" is the required behaviour there; the
        // probe's type dump is what will tell the next session which case actually occurs.
        private static bool TryReadSharedRaceState(object value, out float raceStartTime, out int racers, out string mode)
        {
            raceStartTime = 0f;
            racers = -1;
            mode = null;
            if (value == null) return false;
            try
            {
                Type t = value.GetType();
                PropertyInfo startProp = t.GetProperty("RaceStartTime", BindingFlags.Public | BindingFlags.Instance);
                PropertyInfo racersProp = t.GetProperty("InitialRacersCount", BindingFlags.Public | BindingFlags.Instance);
                if (startProp == null || racersProp == null) return false;
                if (startProp.PropertyType != typeof(float) || racersProp.PropertyType != typeof(int)) return false;

                raceStartTime = (float)startProp.GetValue(value, null);
                racers = (int)racersProp.GetValue(value, null);
                mode = TelemetryModeFromNamespace(t.Namespace);
                return true;
            }
            catch
            {
                raceStartTime = 0f;
                racers = -1;
                mode = null;
                return false;
            }
        }

        // "Liftoff.Multiplayer.ClassicRace" -> "ClassicRace". The namespace survived
        // obfuscation while the manager class names did not, so it is the most stable game-mode
        // discriminator available at this anchor — and it reports what the game is ACTUALLY
        // running, unlike the plugin's own targetGameMode (which is the value it last selected
        // in the settings dropdown).
        private static string TelemetryModeFromNamespace(string ns)
        {
            if (string.IsNullOrEmpty(ns)) return null;
            int dot = ns.LastIndexOf('.');
            string tail = dot >= 0 ? ns.Substring(dot + 1) : ns;
            return string.IsNullOrEmpty(tail) ? null : tail;
        }

        // track/env are REQUIRED fields on both race events, so they are coalesced to "" and
        // never omitted (same convention as admin_command_result). Preferred source is the
        // track the game actually has loaded; the plugin's own last-applied track is the
        // fallback for the window before the flight scene is up.
        private static void ReadTelemetryTrackAndEnv(out string track, out string env)
        {
            track = null;
            env = null;
            try
            {
                if (!TryGetCurrentLoadedTrack(out track, out env) || string.IsNullOrEmpty(track))
                {
                    track = currentTrackName;
                    env = currentEnvironment;
                }
            }
            catch
            {
                track = currentTrackName;
                env = currentEnvironment;
            }
            if (track == null) track = "";
            if (env == null) env = "";
        }

        // Required laps for the loaded race, for race_start's optional `laps`. Same access
        // pattern (and same decompile-confirmed anchors) as TryGetCurrentLoadedTrack in
        // Plugin.Photon.cs: CurrentContentContainer is a readable LugusSingletonExisting<>
        // MonoBehaviour exposing `public Race Race => currentLevel.raceInfo as Race;`, and
        // `Race : RaceQuickInfo` carries the readable public field `[XmlElement("requiredLaps")]
        // public int requiredLaps = 1;`. Returns false (field omitted) if anything is missing.
        private static bool TryGetRequiredLaps(out int laps)
        {
            laps = 0;
            try
            {
                Type containerType = Type.GetType("CurrentContentContainer, Assembly-CSharp");
                if (containerType == null) return false;
                object container = null;
                foreach (var candidate in Resources.FindObjectsOfTypeAll(containerType))
                {
                    if (candidate != null) { container = candidate; break; }
                }
                if (container == null) return false;

                object race = containerType.GetProperty("Race", BindingFlags.Public | BindingFlags.Instance)
                    ?.GetValue(container);
                if (race == null) return false;

                FieldInfo lapsField = race.GetType().GetField("requiredLaps", BindingFlags.Public | BindingFlags.Instance);
                if (lapsField == null || lapsField.FieldType != typeof(int)) return false;

                laps = (int)lapsField.GetValue(race);
                return laps > 0;
            }
            catch
            {
                laps = 0;
                return false;
            }
        }

        private static void LogRaceStartEvent()
        {
            string track, env;
            ReadTelemetryTrackAndEnv(out track, out env);

            int laps;
            object lapsField = TryGetRequiredLaps(out laps) ? (object)laps : null;

            LogJsonEvent("race_start",
                ("track", track),
                ("env", env),
                ("mode", telemetryRaceMode),
                ("racers", telemetryRaceRacers > 0 ? (object)telemetryRaceRacers : null),
                ("laps", lapsField));
        }

        // `standings` and `finishers` are deliberately absent: both need per-player
        // GameModeState parsing, which player-stats-tracking.md owns (spike's implementation
        // split). `cause` is an addition to the spike's field table, recorded in
        // lifecycle-event-logging.md: it names WHICH anchor fired, which is exactly what the
        // next live session has to learn — and it follows the `disconnect` event's precedent of
        // carrying the raw mechanism name rather than a paraphrase.
        private static void LogRaceEndEvent(string cause)
        {
            string track, env;
            ReadTelemetryTrackAndEnv(out track, out env);

            object elapsed = null;
            try
            {
                if (telemetryRaceObservedStart != DateTime.MinValue)
                    elapsed = Math.Round((DateTime.Now - telemetryRaceObservedStart).TotalSeconds, 1);
            }
            catch { /* elapsed_s stays null -> omitted */ }

            LogJsonEvent("race_end",
                ("track", track),
                ("env", env),
                ("mode", telemetryRaceMode),
                ("racers", telemetryRaceRacers > 0 ? (object)telemetryRaceRacers : null),
                ("elapsed_s", elapsed),
                ("cause", cause));

            telemetryRaceOpen = false;
            telemetryRaceObservedStart = DateTime.MinValue;
        }

        // ───────────────────────────────────────────────────────────────────────────────
        // The probe
        // ───────────────────────────────────────────────────────────────────────────────
        private static bool TelemetryProbeEnabled()
        {
            try
            {
                if ((DateTime.Now - telemetryProbeFlagChecked).TotalSeconds < TelemetryProbeFlagCacheSeconds)
                    return telemetryProbeFlagValue;
                telemetryProbeFlagChecked = DateTime.Now;
                telemetryProbeFlagValue = !string.IsNullOrEmpty(pluginPath) &&
                                          File.Exists(Path.Combine(pluginPath, TelemetryProbeFlagFileName));
            }
            catch { /* keep the last known value */ }
            return telemetryProbeFlagValue;
        }

        // One line per observed property update. Answers, in a single live race:
        //   Q1 does the bot receive OTHER players' updates?  -> a `player` line whose target is
        //      not the bot's own nick.
        //   Q3 typed object or raw byte[]?                   -> each value's GetType().FullName
        //      (plus an explicit len= for byte[]).
        //   Q4 duplicate-dispatch rate?                      -> changed=false lines are repeat
        //      dispatches of content already seen for that target.
        //   plus: does the value-shape detector work?        -> shape=... hints, produced by the
        //      same predicates the readers above and player-stats-tracking.md will use.
        // (Q2, RPC delivery, is answered separately by the RPCRaceFinished patch's own log line.)
        private static void LogTelemetryProbe(string kind, string target, string userId, object propsObj, bool changed)
        {
            try
            {
                bool escalated = TelemetryProbeEnabled();
                if (!escalated && telemetryProbeUnityLines >= TelemetryProbeUnityLogCap) return;
                if (!escalated) telemetryProbeUnityLines++;

                string description = DescribeProperties(propsObj);
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] TELEMETRY PROBE {kind} target={(string.IsNullOrEmpty(target) ? "-" : target)} changed={(changed ? "true" : "false")} props=[{description}]");

                if (escalated)
                {
                    LogJsonEvent("telemetry_probe",
                        ("kind", kind),
                        ("player", string.IsNullOrEmpty(target) ? null : target),
                        ("userId", string.IsNullOrEmpty(userId) ? null : userId),
                        ("changed", changed),
                        ("props", description));
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Telemetry probe failed: {ex.Message}");
            }
        }

        // Content signature of a changed-properties Hashtable, used only to answer
        // "same content as last time?". Values are rendered the same way the probe renders
        // them, so a value whose ToString() carries no per-instance data degrades to
        // "unchanged" rather than to a wrong claim — which is why the probe reports its
        // duplicate rate as an observation to confirm live, not as a fact.
        private static string BuildPropertiesSignature(object propsObj)
        {
            IDictionary dict = propsObj as IDictionary;
            if (dict == null) return "<none>";
            var parts = new List<string>();
            try
            {
                foreach (DictionaryEntry entry in dict)
                {
                    parts.Add((entry.Key == null ? "" : entry.Key.ToString()) + "=" + DescribeValue(entry.Value));
                }
            }
            catch { /* partial signature is still a usable signature */ }
            parts.Sort(StringComparer.Ordinal);
            return string.Join("|", parts.ToArray());
        }

        private static string DescribeProperties(object propsObj)
        {
            string description = BuildPropertiesSignature(propsObj);
            if (description.Length > TelemetryProbeDescriptionMaxChars)
                description = description.Substring(0, TelemetryProbeDescriptionMaxChars) + "…(truncated)";
            return description;
        }

        // Renders a property value as "<type>[:<detail>][:shape=<hint>]". Primitives carry
        // their value; a byte[] carries its length (the tell-tale of the un-deserialized case);
        // everything else carries its runtime type name plus, when recognised, a shape hint.
        private static string DescribeValue(object value)
        {
            if (value == null) return "null";
            try
            {
                Type t = value.GetType();
                if (value is string s) return "String:" + (s.Length > 60 ? s.Substring(0, 60) + "…" : s);
                if (value is byte[] bytes) return "Byte[]:len=" + bytes.Length.ToString(CultureInfo.InvariantCulture);
                if (t.IsPrimitive || t.IsEnum)
                    return t.Name + ":" + Convert.ToString(value, CultureInfo.InvariantCulture);

                string shape = DescribeValueShape(t);
                return t.FullName + (shape == null ? "" : ":shape=" + shape);
            }
            catch
            {
                return "<unreadable>";
            }
        }

        // The value-shape detector, in diagnostic form. Each predicate is the same one the
        // corresponding reader uses (or, for game_mode_state, the one the spike prescribes for
        // player-stats-tracking.md), so a probe line that reports a shape is direct evidence
        // that the reader keyed on that shape will work at runtime — and a line that reports
        // none for a value that should have matched is direct evidence that it will not.
        private static string DescribeValueShape(Type t)
        {
            try
            {
                if (t.GetProperty("RaceStartTime", BindingFlags.Public | BindingFlags.Instance) != null &&
                    t.GetProperty("InitialRacersCount", BindingFlags.Public | BindingFlags.Instance) != null)
                    return "shared_race_state";

                if (t.Name.IndexOf("RacePlayerCheckpointInfo", StringComparison.Ordinal) >= 0)
                    return "checkpoint_info";

                // GameModeState (spike §3): the state object owns a nested/field enum whose
                // members include both Crashed and Finished. Those enum MEMBER names survived
                // obfuscation; the enum's own type name did not.
                foreach (FieldInfo f in t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance))
                {
                    if (!f.FieldType.IsEnum) continue;
                    string[] names = Enum.GetNames(f.FieldType);
                    if (names.Contains("Crashed") && names.Contains("Finished")) return "game_mode_state";
                }
            }
            catch { /* an unreadable type simply has no hint */ }
            return null;
        }

        // ───────────────────────────────────────────────────────────────────────────────
        // Harmony patches owned by this file (applied from ApplyHarmonyPatches)
        // ───────────────────────────────────────────────────────────────────────────────
        //
        // 1. System chat lines. The plugin only ever patched ChatWindowPanel.GenerateUserMessage,
        //    so every non-user chat entry was invisible to it. Decompile of
        //    Liftoff.Multiplayer.Chat.ChatWindowPanel (Liftoff 1.7.4) confirms its receive
        //    handler dispatches to exactly three renderers:
        //        private void GenerateUserMessage(string userId, string userName, string message, Color ledColor)  // already patched
        //        private void GenerateSystemMessageEntry(string msg)
        //        private void GenerateSystemMessageForPlayer(string msg, <obfuscated player wrapper> player)
        //    Both system renderers are patched here as PREFIXES that only read and return void
        //    (so the original always runs). A prefix — rather than the postfix ChatMessagePatch
        //    uses — because `object[] __args` injection is already proven in this codebase on a
        //    prefix (PhotonContainerPrefix), and __args is the only way to reach a parameter
        //    whose type is obfuscated and therefore unnameable at compile time.
        //
        // 2. RPCRaceFinished. All 25 [PunRPC] method names survived obfuscation; exactly two
        //    declarations of `private void RPCRaceFinished()` exist in Assembly-CSharp (the
        //    Classic Race and Dropout Race managers), and the sender calls
        //    `RpcSecure(<RPCRaceFinished>, (RpcTarget)0, true, Array.Empty<object>())`, i.e. it
        //    is broadcast rather than targeted. Whether PUN actually dispatches it into the
        //    bot's own manager instance is spike reachability question 2 and cannot be settled
        //    by reading IL — if it never fires, this simply produces no race_end from this
        //    anchor and the room-property triggers remain. Nothing else changes.
        private static void ApplyTelemetryPatches(Assembly asm)
        {
            try
            {
                var harmony = new HarmonyLib.Harmony("com.barhoomi.liftoff.autolobby.telemetry");

                Type chatType = FindType("Liftoff.Multiplayer.Chat.ChatWindowPanel");
                if (chatType != null)
                {
                    MethodInfo systemEntry = chatType.GetMethod("GenerateSystemMessageEntry",
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance,
                        null, new Type[] { typeof(string) }, null);
                    TryApplyTelemetryPrefix(harmony, systemEntry, "SystemMessageEntryPrefix",
                        "ChatWindowPanel.GenerateSystemMessageEntry(string)");

                    // The second parameter's type is obfuscated, so the method is resolved by
                    // name + arity + first-parameter type rather than by a full signature match.
                    MethodInfo systemForPlayer = chatType
                        .GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                        .FirstOrDefault(m => m.Name == "GenerateSystemMessageForPlayer"
                                             && m.GetParameters().Length == 2
                                             && m.GetParameters()[0].ParameterType == typeof(string));
                    TryApplyTelemetryPrefix(harmony, systemForPlayer, "SystemMessageForPlayerPrefix",
                        "ChatWindowPanel.GenerateSystemMessageForPlayer(string, <player>)");
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] ChatWindowPanel type not found — system chat telemetry disabled this session.");
                }

                int racePatched = 0;
                foreach (Type t in asm.GetTypes())
                {
                    MethodInfo m;
                    try
                    {
                        m = t.GetMethod("RPCRaceFinished",
                            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance,
                            null, Type.EmptyTypes, null);
                    }
                    catch { continue; }
                    if (m == null || m.DeclaringType != t) continue;
                    if (TryApplyTelemetryPrefix(harmony, m, "RaceFinishedPrefix", $"{t.Name}::RPCRaceFinished()")) racePatched++;
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Patched {racePatched} RPCRaceFinished method(s) for race_end telemetry.");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Telemetry patching failed: {ex.Message}");
            }
        }

        // One patch attempt, fully isolated: a missing target, a missing prefix method, or a
        // Harmony failure on one method must never abort the remaining telemetry patches (and
        // ApplyTelemetryPatches itself runs last in ApplyHarmonyPatches, so it can never affect
        // the gameplay-critical patches either). Returns true ONLY when Harmony actually applied
        // the patch — a non-null MethodInfo alone is never reported as success (AGENTS.md rule 2).
        private static bool TryApplyTelemetryPrefix(HarmonyLib.Harmony harmony, MethodInfo target, string prefixName, string label)
        {
            if (target == null)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Telemetry target not found: {label} — that signal will not be logged this session.");
                return false;
            }
            MethodInfo prefix = typeof(AutoLobbyPlugin).GetMethod(prefixName, BindingFlags.NonPublic | BindingFlags.Static);
            if (prefix == null)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Telemetry prefix method {prefixName} not found in plugin — {label} not patched.");
                return false;
            }
            try
            {
                harmony.Patch(target, prefix: new HarmonyLib.HarmonyMethod(prefix));
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Telemetry patch applied: {label}.");
                return true;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Telemetry patch FAILED for {label}: {ex.Message}");
                return false;
            }
        }

        // Same replay guard ChatMessagePatch applies: ChatWindowPanel.GenerateChatFromHistory
        // re-renders the stored chat history every time the panel is enabled, and it calls the
        // very same three renderers (confirmed in the decompile). Without this, re-entering a
        // room would re-emit every past system line. Duplicated rather than shared because
        // ChatMessagePatch.IsRenderingHistory is a private member of a nested type, which the
        // enclosing type cannot reach.
        private static bool IsRenderingChatHistory()
        {
            try
            {
                return System.Environment.StackTrace.IndexOf("GenerateChatFromHistory", StringComparison.OrdinalIgnoreCase) >= 0;
            }
            catch
            {
                return false;
            }
        }

        private static void SystemMessageEntryPrefix(object[] __args)
        {
            try
            {
                if (__args == null || __args.Length < 1) return;
                LogSystemChatEvent("system", __args[0] as string, null);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] SystemMessageEntryPrefix failed: {ex.Message}");
            }
        }

        private static void SystemMessageForPlayerPrefix(object[] __args)
        {
            try
            {
                if (__args == null || __args.Length < 2) return;
                LogSystemChatEvent("system_for_player", __args[0] as string, __args[1]);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] SystemMessageForPlayerPrefix failed: {ex.Message}");
            }
        }

        // `system_chat` — one JSONL line per system chat entry the game renders.
        //   kind : "system"            <- ChatWindowPanel.GenerateSystemMessageEntry(string)
        //          "system_for_player" <- ChatWindowPanel.GenerateSystemMessageForPlayer(string, player)
        //   msg  : the RAW text, unmodified (no trimming beyond whitespace, no parsing) —
        //          the point of this event is to capture what the game says verbatim.
        //   player/userId : only for the for-player variant, and only when readable.
        //
        // Operator-relevant (spike, "Operator correction 2026-08-05"): the operator has seen
        // Liftoff announce "new all time record" / "new monthly record" in chat. The spike
        // proved those cannot arrive through GenerateUserMessage (the only chat hook the plugin
        // had), so if they are chat at all they must arrive through one of these two renderers.
        // The next live session should grep the JSONL for `"event":"system_chat"` and look for
        // the record wording — that is the evidence that settles where those messages come from.
        private static void LogSystemChatEvent(string kind, string message, object playerWrapper)
        {
            if (message == null) return;
            if (IsRenderingChatHistory()) return;

            string nick = null, userId = null;
            if (playerWrapper != null)
            {
                // The wrapper type is obfuscated but decompile-confirmed to expose the raw
                // Photon player: `private readonly Player <field>; public Player <prop> => <field>;`
                // Resolved by TYPE (Photon.Realtime.Player), never by name.
                try
                {
                    Type wrapperType = playerWrapper.GetType();
                    object photonPlayer = wrapperType
                        .GetProperties(BindingFlags.Public | BindingFlags.Instance)
                        .Where(p => p.PropertyType == typeof(Player) && p.GetIndexParameters().Length == 0)
                        .Select(p => p.GetValue(playerWrapper, null))
                        .FirstOrDefault(v => v != null);
                    if (photonPlayer == null)
                    {
                        photonPlayer = wrapperType
                            .GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                            .Where(f => f.FieldType == typeof(Player))
                            .Select(f => f.GetValue(playerWrapper))
                            .FirstOrDefault(v => v != null);
                    }
                    if (photonPlayer != null) ReadPhotonPlayerInfo(photonPlayer, out nick, out userId);
                }
                catch { /* identity stays null -> those fields are omitted */ }
            }

            UnityEngine.Debug.Log($"[AutoLobbyPlugin] System chat ({kind}): {message}");
            LogJsonEvent("system_chat",
                ("kind", kind),
                ("msg", message),
                ("player", string.IsNullOrEmpty(nick) ? null : nick),
                ("userId", string.IsNullOrEmpty(userId) ? null : userId));
        }

        // Prefix on the game's own [PunRPC] RPCRaceFinished(). Returns void, so the original
        // always runs. Only emits when a race is actually open, so an RPC that arrives without
        // an observed race start (or a second copy of the same one) can never invent an event.
        private static void RaceFinishedPrefix()
        {
            try
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] RPCRaceFinished observed (race open: {telemetryRaceOpen}).");
                if (telemetryRaceOpen) LogRaceEndEvent("rpc_race_finished");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] RaceFinishedPrefix failed: {ex.Message}");
            }
        }
    }
}
