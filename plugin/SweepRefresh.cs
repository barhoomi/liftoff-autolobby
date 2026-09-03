using System;
using System.IO;

namespace LiftoffAutoLobby
{
    // MODE: shared
    // Forced re-run of the Environment x GameMode availability sweep, without a restart
    // (docs/features/doing/workshop-ingest-hardening.md §2).
    //
    // The problem it solves: TryReuseCachedTrackModeDump reuses a previous session's dump
    // whenever the set of ENVIRONMENT NAMES matches, so a brand-new track inside an existing
    // environment never invalidates it -- the dump on the live box went a month stale across
    // many restarts. A freshly downloaded workshop track therefore stayed invisible to
    // gather_tracks.py (whose dump-authoritative branch rebuilds the master list only from
    // ui_tracks_dump.json), which is exactly why the 2026-09-03 session needed a hand-deleted
    // cache file plus a restart.
    //
    // Why arming a flag is enough, and no synthetic room-setup pass is needed: the sweep lives
    // inside ConfigureAndCreateRoom (Plugin.RoomSetup.cs), whose only live call site
    // (Plugin.cs) is reached whenever the settings popup is open -- and rotation re-opens that
    // popup every cycle by clicking CHANGE SETTINGS (Plugin.GameRoom.cs). So the dump gate is
    // re-evaluated every rotation and only short-circuits because trackModeDumpDoneThisSession
    // is already true. Clearing that is the whole mechanism.
    //
    // Deliberately NOT "delete track_mode_availability.json to force a re-dump": the control
    // plane's cross_validate_tracks fails OPEN on a missing file (dashboard/control/
    // playlists.py), so between the delete and the next dump every mode cross-check would
    // silently stop filtering.
    //
    // Own file, nested in AutoLobbyPlugin, same shape as WorkshopDownloader.cs: it reaches the
    // plugin's private statics (pluginPath, LogJsonEvent) without widening their visibility,
    // and keeps the edit to the high-contention shared files down to one line.
    public partial class AutoLobbyPlugin
    {
        public static class SweepRefresh
        {
            // Presence-only one-shot request (the skip_now.txt convention): touching this file
            // in the plugins directory forces one full re-sweep on the next room-setup pass.
            // Content is ignored -- there is nothing to say beyond "do it".
            public const string RequestFileName = "sweep_refresh_request.txt";

            // Rate limit on the "re-armed" log line only; arming itself is always idempotent.
            private const double ArmLogIntervalSeconds = 5.0;

            private static bool armed;
            private static DateTime lastArmLogUtc = DateTime.MinValue;

            /// <summary>
            /// Ask for one full availability re-sweep on the next room-setup pass. Idempotent:
            /// arming while already armed still produces exactly one re-sweep, and logs at most
            /// once per 5s.
            /// </summary>
            public static void Arm(string why)
            {
                bool alreadyArmed = armed;
                armed = true;

                DateTime now = DateTime.UtcNow;
                if (alreadyArmed && (now - lastArmLogUtc).TotalSeconds < ArmLogIntervalSeconds) return;
                lastArmLogUtc = now;

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Availability sweep re-armed ({why}) — the next room-setup pass will re-dump.");
                LogJsonEvent("sweep_refresh_armed", ("reason", why));
            }

            /// <summary>Returns whether a re-sweep was armed, and clears the flag.</summary>
            public static bool ConsumeArmed()
            {
                bool wasArmed = armed;
                armed = false;
                return wasArmed;
            }

            // Called once per second from RunTick (Plugin.cs), above the scene dispatch so the
            // flag can be armed in any scene. Cheap when idle: one File.Exists.
            public static void Tick()
            {
                try
                {
                    if (string.IsNullOrEmpty(pluginPath)) return;

                    string requestPath = Path.Combine(pluginPath, RequestFileName);
                    if (!File.Exists(requestPath)) return;

                    // Delete before acting, same discipline as WorkshopDownloader's request
                    // file: a request that survives the action it triggered would be replayed
                    // forever, and a re-sweep costs a rotation's worth of latency each time.
                    try { File.Delete(requestPath); }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to delete {RequestFileName}: {ex.Message} -- refusing to process it, it would be reprocessed forever.");
                        return;
                    }

                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Availability sweep refresh requested via {RequestFileName}.");
                    Arm("request_file");
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] SweepRefresh.Tick failed: {ex}");
                }
            }
        }
    }
}
