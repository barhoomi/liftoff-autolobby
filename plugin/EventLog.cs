using System;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;

namespace LiftoffAutoLobby
{
    // Structured JSON event log — plugin half of roadmap item A3
    // (docs/features/doing/structured-logging.md).
    //
    // This is a SEPARATE, ADDITIVE sink from LogEvent() in Plugin.cs. LogEvent emits the
    // bare Unity-log slice ("[AutoLobbyPlugin:EVENT] {\"event\":...}") that the black-box
    // scenario harness greps; do NOT conflate the two. This sink appends one enveloped
    // JSON line per event to the SAME daily file the orchestrator writes
    // (<log_dir>/bot-YYYY-MM-DD.jsonl), distinguished by "source":"plugin", so a single
    // consumer can read both halves from one file.
    public partial class AutoLobbyPlugin
    {
        // Envelope + shared-writer discipline (see the feature-doc "Canonical Event Schema"):
        //  - File: <log_dir>/bot-YYYY-MM-DD.jsonl, UTC calendar date chosen from the SAME
        //    instant as the record's ts, so an event never lands in a mismatched daily file.
        //  - One JSON object per line, '\n'-terminated, UTF-8, no pretty-print.
        //  - Append mode (FileMode.Append -> O_APPEND on Unix), one write per event. POSIX
        //    guarantees sub-PIPE_BUF (4 KiB) append writes are atomic, so the plugin and the
        //    orchestrator co-write one file without locking. We never truncate/rewrite.
        //  - Envelope key order is exactly: ts, source, event. Then event-specific payload.
        //  - null payload values are OMITTED (never written as null).
        //
        // Note the hard C# gotcha: in a custom DateTime format string the letters 'T' and 'Z'
        // are NOT literals — an unquoted Z misbehaves. They are single-quoted below so the
        // output is exactly like "2026-07-05T11:11:06Z".
        private const string EventTsFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'";
        private const string EventDateFormat = "yyyy-MM-dd";

        // Reused, BOM-free UTF-8 encoder. A BOM appended mid-file would corrupt the JSONL,
        // so we never emit one.
        private static readonly UTF8Encoding EventLogEncoding = new UTF8Encoding(false);

        // Resolve the shared log directory. Single resolver (CLAUDE rule #4): prefer the
        // absolute path the ORCHESTRATOR already resolved and wrote to log_dir.txt — the
        // plugin must never re-derive the directory a second, possibly-disagreeing way.
        // Fallbacks only apply when the plugin runs without an orchestrator present:
        // FPV_LOG_DIR env (the Docker volume hook), then a logs/ dir next to the plugin.
        // Read fresh each call (events are human-paced, cost is negligible) so the plugin
        // starting before the orchestrator writes log_dir.txt self-heals on the next event.
        private static string ResolveEventLogDir()
        {
            try
            {
                if (!string.IsNullOrEmpty(pluginPath))
                {
                    string stateFile = Path.Combine(pluginPath, "log_dir.txt");
                    if (File.Exists(stateFile))
                    {
                        string configured = File.ReadAllText(stateFile).Trim();
                        if (!string.IsNullOrEmpty(configured)) return configured;
                    }
                }
            }
            catch { /* fall through to env / default */ }

            try
            {
                string env = System.Environment.GetEnvironmentVariable("FPV_LOG_DIR");
                if (!string.IsNullOrEmpty(env)) return env;
            }
            catch { /* fall through to default */ }

            try
            {
                if (!string.IsNullOrEmpty(pluginPath)) return Path.Combine(pluginPath, "logs");
            }
            catch { /* give up */ }

            return null;
        }

        // Render a payload value as JSON. bool -> true/false, integral/floating -> bare
        // number (matching the orchestrator's json.dumps output for typed fields); anything
        // else is treated as a JSON string and escaped with the same JsonEscapeStrict the
        // Unity-log slice uses (handles user-supplied chat text). Callers omit null values
        // before reaching here.
        private static void AppendJsonValue(StringBuilder sb, object value)
        {
            switch (value)
            {
                case bool b:
                    sb.Append(b ? "true" : "false");
                    break;
                case int i:
                    sb.Append(i.ToString(CultureInfo.InvariantCulture));
                    break;
                case long l:
                    sb.Append(l.ToString(CultureInfo.InvariantCulture));
                    break;
                case double d:
                    sb.Append(d.ToString(CultureInfo.InvariantCulture));
                    break;
                case float f:
                    sb.Append(f.ToString(CultureInfo.InvariantCulture));
                    break;
                default:
                    sb.Append('"').Append(JsonEscapeStrict(value.ToString())).Append('"');
                    break;
            }
        }

        // Append one enveloped event line to the shared daily JSONL file. All IO is wrapped:
        // a failure warns to the Unity log and never throws into the 1-second game tick
        // (logging must never take down the control loop — mirrors the orchestrator's
        // _NullLogger discipline). Named distinctly from LogEvent so the (string,object)[]
        // typed-payload overload can never be confused with LogEvent's (string,string)[].
        private static void LogJsonEvent(string eventName, params (string key, object value)[] fields)
        {
            try
            {
                DateTime now = DateTime.UtcNow;
                string dir = ResolveEventLogDir();
                if (string.IsNullOrEmpty(dir)) return;
                Directory.CreateDirectory(dir);

                string file = Path.Combine(dir, "bot-" + now.ToString(EventDateFormat, CultureInfo.InvariantCulture) + ".jsonl");

                var sb = new StringBuilder();
                sb.Append("{\"ts\":\"").Append(now.ToString(EventTsFormat, CultureInfo.InvariantCulture))
                  .Append("\",\"source\":\"plugin\",\"event\":\"").Append(JsonEscapeStrict(eventName)).Append('"');

                if (fields != null)
                {
                    foreach (var f in fields)
                    {
                        if (f.value == null) continue; // omit null/absent optional fields
                        sb.Append(",\"").Append(JsonEscapeStrict(f.key)).Append("\":");
                        AppendJsonValue(sb, f.value);
                    }
                }
                sb.Append("}\n");

                File.AppendAllText(file, sb.ToString(), EventLogEncoding);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] LogJsonEvent write failed: {ex.Message}");
            }
        }

        // Best-effort reflection read of a Photon.Realtime.Player's NickName + UserId, for the
        // player_join / player_leave events. Confirmed via decompile of PhotonRealtime.dll
        // (Player.NickName : string, Player.UserId : string) per CLAUDE rule #1 — not guessed.
        // Mirrors the reflection style already used in KickPlayer. Never throws.
        private static void ReadPhotonPlayerInfo(object playerObj, out string nick, out string userId)
        {
            nick = null;
            userId = null;
            if (playerObj == null) return;
            try
            {
                Type t = playerObj.GetType();
                PropertyInfo nickProp = t.GetProperty("NickName") ?? t.GetProperty("Nickname");
                if (nickProp != null) nick = nickProp.GetValue(playerObj, null) as string;
                PropertyInfo userIdProp = t.GetProperty("UserId");
                if (userIdProp != null) userId = userIdProp.GetValue(playerObj, null) as string;
            }
            catch { /* best-effort; leave whichever fields we couldn't read as null (omitted) */ }
        }

        // Emit a player_join / player_leave file event for a Photon Player arg from the
        // callback-dispatch prefix. count (current room player count) is optional — omitted
        // if the room can't be read. Reuses the existing TryGetRoomInfo reflection reader.
        //
        // lifecycle-event-logging.md, "Observed quirk": the live JSONL showed a second,
        // identity-less line right after a real join —
        //   {"event":"player_join","player":"futurehasnomercy","userId":"steam_...","count":2}
        //   {"event":"player_join","player":"","count":2}
        // Decompile of PhotonRealtime.dll (per CLAUDE rule #1, not guessed) settles where that
        // comes from: InRoomCallbacksContainer.OnPlayerEnteredRoom is the ONLY dispatch method
        // for this callback, it is a plain `List<IInRoomCallbacks>` loop with no fan-out to
        // sibling containers, and LoadBalancingClient calls it from exactly ONE site
        // (OnEvent, EventCode.Join == 255, the `sender != LocalPlayer.ActorNumber` branch). So
        // one dispatch == one prefix firing: a second line is a second *dispatch*, not a
        // double-logging bug. In that same call site, a Player the room has not stored yet is
        // built by `CreatePlayer(string.Empty, sender, isLocal: false, actorProperties)` — which
        // yields exactly NickName == "" / UserId == null when the join event carried no
        // nickname/userId properties. That is the empty line: a dispatch that carries no
        // identity at all, and therefore no information a JSONL consumer can use.
        // We drop those from the JSONL (they would inflate join/leave counts for the next
        // investigation) but still surface them in the Unity log, so nothing becomes invisible.
        private static void LogPlayerPresenceEvent(string eventName, object playerObj)
        {
            string nick, userId;
            ReadPhotonPlayerInfo(playerObj, out nick, out userId);

            if (string.IsNullOrEmpty(nick) && string.IsNullOrEmpty(userId))
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Skipping identity-less {eventName} JSONL line (Photon dispatched a Player with no nick and no userId).");
                return;
            }

            object count = null;
            try
            {
                bool isVisible; string roomName; int maxPlayers, playerCount;
                if (TryGetRoomInfo(out isVisible, out roomName, out maxPlayers, out playerCount))
                    count = playerCount;
            }
            catch { /* count stays null -> omitted */ }

            LogJsonEvent(eventName, ("player", nick), ("userId", userId), ("count", count));
        }

        // Emit a `disconnect` file event from the Photon callback-dispatch prefix
        // (lifecycle-event-logging.md). MUST be called BEFORE the prefix resets roomCreatedTime,
        // since elapsed_s is the whole point of the event: it shows "the bot dropped at 291s of a
        // 300s configured rotation" in one line, which is what `lobby-room-churn-on-rotation.md`
        // had to reconstruct from ~83,000 lines of Player.log by hand.
        //
        // `cause` is the raw Photon callback name, never a paraphrase, so the JSONL greps against
        // Player.log's own callback names. The two causes are NOT duplicates of each other and a
        // consumer must not collapse them — decompile of LoadBalancingClient:
        //   OnLeftRoom      — one call site, inside the StatusCode.Disconnect handler, gated on
        //                     `Server == GameServer && CurrentRoom != null` with CurrentRoom
        //                     nulled immediately before the call. It therefore fires on EVERY room
        //                     exit, including a perfectly graceful leave (leaving the game server
        //                     is how Realtime leaves a room), and cannot fire twice until the
        //                     client has re-entered a room.
        //   OnDisconnected  — two call sites, but they are mutually exclusive branches of one
        //                     `switch (State)` in the same handler, so at most one fires per
        //                     StatusCode.Disconnect, and only when the client actually lands in
        //                     ClientState.Disconnected.
        // That is why the churn investigation saw 92 OnLeftRoom vs 54 OnDisconnected: every
        // rotation/room recreate contributes an OnLeftRoom, only genuine drops add an
        // OnDisconnected. Neither is double-fired, so no de-dup guard is applied here.
        private static void LogDisconnectEvent(string cause)
        {
            object elapsedSeconds = null;
            try
            {
                // Both sentinels mean "no meaningful room timer": MinValue = not in a room,
                // MaxValue = timer frozen (settings update pending / rotation paused). Omit
                // rather than emit a nonsense elapsed.
                if (roomCreatedTime != DateTime.MinValue && roomCreatedTime != DateTime.MaxValue)
                    elapsedSeconds = Math.Round((DateTime.Now - roomCreatedTime).TotalSeconds, 1);
            }
            catch { /* elapsed_s stays null -> omitted */ }

            object configuredInterval = null;
            try { configuredInterval = GetRotationInterval(); }
            catch { /* configured_interval_s stays null -> omitted */ }

            LogJsonEvent("disconnect",
                ("cause", cause),
                ("elapsed_s", elapsedSeconds),
                ("configured_interval_s", configuredInterval));
        }

        // Emit an `admin_command_result` file event (lifecycle-event-logging.md). One line per
        // CommandRegistry.Process outcome, so a JSONL-only reader can tell "admin ran /interval
        // and it took effect" from "a non-admin's /interval was silently ignored" from "admin ran
        // it but the bot doesn't own the room" — the three states `container-seed-admin-ids.md`
        // had to disambiguate with a docker exec plus a code read.
        //
        // NOTE the deliberate split between the two logging sinks (structured-logging.md): the
        // pre-existing `LogEvent("chat_command", ...)` in Process is the Unity-log slice ONLY
        // (LogEvent just Debug.Logs "[AutoLobbyPlugin:EVENT] {...}" for the scenario harness to
        // grep) — it never reaches the JSONL. So this is genuinely net-new information in the
        // file log, not a duplicate of an existing call.
        //
        // cmd/user_name/user_id/result are all REQUIRED fields, so they are coalesced to "" and
        // never omitted; LogJsonEvent's omit-on-null rule stays reserved for optional fields.
        private static void LogAdminCommandResult(string cmd, string userName, string userId, string result)
        {
            LogJsonEvent("admin_command_result",
                ("cmd", cmd ?? ""),
                ("user_name", userName ?? ""),
                ("user_id", userId ?? ""),
                ("result", result));
        }
    }
}
