using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace LiftoffAutoLobby
{
    // MODE: shared
    // In-game Steam Workshop download (docs/features/doing/workshop-ingame-download.md).
    //
    // Nested in AutoLobbyPlugin (same pattern as CommandRegistry / Commands/*.cs) so it can
    // reach the plugin's private statics (pluginPath, QueueChatMessage, LogJsonEvent) without
    // widening their visibility. Deliberately its own FILE rather than another Plugin.<Area>.cs
    // partial: the tick call site in Plugin.cs is the only edit this feature makes to the
    // high-contention shared files (see the feature doc's "Coordination constraints").
    //
    // Why the callback and not the return value (AGENTS.md rule 2/3, and the decompile evidence
    // in docs/features/done/workshop-ingame-download-spike.md):
    //   - SteamUGC.DownloadItem's bool only means "the download request was accepted"; it says
    //     nothing about whether any bytes arrived. The ONLY success signal is the Steamworks
    //     DownloadItemResult_t callback (CallbackIdentity 3406), which also carries the failure
    //     EResult that DownloadItem's bool cannot express.
    //   - Callback<T> dispatch is global and multicast: the game's own PlayerCreatedContentService
    //     has its own registrations in the same process, so ours fires alongside (and, per spike
    //     Q4, the game refreshing its own content cache off ItemInstalled_t is what makes the new
    //     track visible without a restart -- we do not reimplement that).
    //   - Steamworks.Callback<T>'s finalizer UNREGISTERS the callback. A local reference would be
    //     collected and the subscription would silently die, so the instance lives in a static
    //     field for the process lifetime (the game's own service keeps its callbacks alive the
    //     same way -- spike Q2).
    //
    // This class NEVER calls SteamAPI.Init()/Shutdown(): Plugin.cs already initializes Steam once
    // on the first tick, and a Shutdown() here would tear down the game's own Steam session
    // (spike "Open risks").
    public partial class AutoLobbyPlugin
    {
        public static class WorkshopDownloader
        {
            // File protocol (registered in dashboard/control/protocol.py's ownership tables):
            //   workshop_download_request.txt  written by the control plane, ONE decimal
            //     published-file id. Deleted by the plugin the instant it starts processing it:
            //     a crash mid-download is unrecoverable from the file's point of view either way,
            //     and a request file surviving a crash would otherwise be silently reprocessed on
            //     the next launch with no memory of the earlier attempt.
            //   workshop_download_result.txt   written by the plugin on completion, one line
            //     "<published_file_id>|<ok|fail>|<reason>"; reason is empty on success (or
            //     "already_installed"), else bad_id / download_rejected / <EResult name> / timeout.
            //     Consumed (read + deleted) by the control plane.
            public const string RequestFileName = "workshop_download_request.txt";
            public const string ResultFileName = "workshop_download_result.txt";

            // Matches the game's own PopupShareContent.RoutineCheckItemUpdateProgress budget
            // (120s, found in the spike's decompile) rather than inventing a new number.
            public const double TimeoutSeconds = 120.0;

            // Liftoff's Steam AppID, confirmed in the decompile via
            // SteamUGC.CreateItem((AppId_t)410340u, ...) -- spike Q3/Q5.
            private const uint LiftoffAppId = 410340u;

            private const double ProgressLogIntervalSeconds = 15.0;

            // Long-lived, never null once registered: see the class comment (finalizer unregisters).
            private static Steamworks.Callback<Steamworks.DownloadItemResult_t> downloadResultCallback;

            // Keyed by PublishedFileId_t.m_PublishedFileId so OnDownloadItemResult can match an
            // async completion back to the request that started it. The file protocol only allows
            // one request in flight at a time, but this structure deliberately does not assume
            // that (a /dl command can be issued while a file-protocol request is running).
            private static readonly Dictionary<ulong, PendingDownload> pending =
                new Dictionary<ulong, PendingDownload>();

            private class PendingDownload
            {
                public ulong Id;
                public DateTime StartedUtc;
                public DateTime LastProgressLogUtc;
                public bool AnnounceInChat;   // /dl: post the outcome to chat when it resolves
                public string RequestedBy;    // chat display name, for the follow-up message
            }

            // Called once per second from RunTick (Plugin.cs). Cheap when idle: one File.Exists.
            public static void Tick()
            {
                try
                {
                    PollRequestFile();
                    ServicePending();
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] WorkshopDownloader.Tick failed: {ex}");
                }
            }

            private static void PollRequestFile()
            {
                if (string.IsNullOrEmpty(pluginPath)) return;

                // One at a time: while a download is in flight the request file is left untouched,
                // so a second request simply waits its turn instead of clobbering the single
                // result file. No new reason code, no queue state to keep in sync.
                if (pending.Count > 0) return;

                string requestPath = Path.Combine(pluginPath, RequestFileName);
                if (!File.Exists(requestPath)) return;

                string raw;
                try
                {
                    raw = File.ReadAllText(requestPath).Trim();
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to read {RequestFileName}: {ex.Message}");
                    return; // transient read error (e.g. mid-replace): retry on the next tick
                }

                // Delete before acting, not after -- see the protocol comment above.
                try { File.Delete(requestPath); }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to delete {RequestFileName}: {ex.Message} -- refusing to process it, it would be reprocessed forever.");
                    return;
                }

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop download requested via {RequestFileName}: '{raw}'");
                TryStartDownload(raw, announceInChat: false, requestedBy: null);
            }

            /// <summary>
            /// Start a workshop download. Returns true when the request reached Steam and a
            /// callback is now awaited; false when it resolved immediately (bad id, already
            /// installed, or Steam refused it) -- in which case the result file has already
            /// been written. NEVER treat true as "the download succeeded".
            /// </summary>
            public static bool TryStartDownload(string publishedFileId, bool announceInChat, string requestedBy)
            {
                ulong raw;
                if (string.IsNullOrEmpty(publishedFileId) ||
                    !ulong.TryParse(publishedFileId.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out raw) ||
                    raw == 0)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Workshop download rejected: '{publishedFileId}' is not a published-file id.");
                    Complete(publishedFileId == null ? "" : publishedFileId.Trim(), false, "bad_id", announceInChat, requestedBy);
                    return false;
                }

                // The game's own service converts exactly this way (spike Q3, decompiled verbatim:
                // `return (PublishedFileId_t)ulong.Parse(s);`) -- same cast, same DLL.
                Steamworks.PublishedFileId_t id = (Steamworks.PublishedFileId_t)raw;

                // Already on disk? Mirrors the game's own state check (spike Q2/Q4) and avoids a
                // redundant round-trip for an item subscribed in a previous session.
                uint state = Steamworks.SteamUGC.GetItemState(id);
                bool installed = (state & (uint)Steamworks.EItemState.k_EItemStateInstalled) != 0;
                bool needsUpdate = (state & (uint)Steamworks.EItemState.k_EItemStateNeedsUpdate) != 0;
                if (installed && !needsUpdate)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop item {raw} is already installed (state=0x{state:X}) -- nothing to download.");
                    Complete(raw.ToString(CultureInfo.InvariantCulture), true, "already_installed", announceInChat, requestedBy);
                    return false;
                }

                // Register BEFORE asking for the download: the result callback can in principle
                // arrive before DownloadItem returns, and a late registration would miss it.
                EnsureCallbackRegistered();

                bool accepted = Steamworks.SteamUGC.DownloadItem(id, true);
                if (!accepted)
                {
                    // The spike found no case where this should happen once Steam is running --
                    // but "should not happen" is not "cannot happen" (AGENTS.md rule 2).
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] SteamUGC.DownloadItem({raw}) returned false -- request refused by Steam.");
                    Complete(raw.ToString(CultureInfo.InvariantCulture), false, "download_rejected", announceInChat, requestedBy);
                    return false;
                }

                pending[raw] = new PendingDownload
                {
                    Id = raw,
                    StartedUtc = DateTime.UtcNow,
                    LastProgressLogUtc = DateTime.UtcNow,
                    AnnounceInChat = announceInChat,
                    RequestedBy = requestedBy,
                };
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] SteamUGC.DownloadItem({raw}) accepted -- awaiting DownloadItemResult_t (timeout {TimeoutSeconds:F0}s).");
                LogJsonEvent("workshop_download_started",
                    ("id", raw.ToString(CultureInfo.InvariantCulture)),
                    ("requested_by", requestedBy));
                return true;
            }

            private static void EnsureCallbackRegistered()
            {
                if (downloadResultCallback != null) return;
                downloadResultCallback = Steamworks.Callback<Steamworks.DownloadItemResult_t>.Create(OnDownloadItemResult);
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Registered Callback<DownloadItemResult_t> (kept in a static field so the finalizer can't unregister it).");
            }

            private static void OnDownloadItemResult(Steamworks.DownloadItemResult_t cb)
            {
                try
                {
                    ulong id = cb.m_nPublishedFileId.m_PublishedFileId;
                    // The callback is global/multicast: the game's own handlers get it too, and a
                    // download we never asked for (anything else in this Steam session) fires it
                    // as well. Anything that isn't ours is not our business.
                    PendingDownload req;
                    if (!pending.TryGetValue(id, out req))
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Ignoring DownloadItemResult_t for {id} (app {cb.m_unAppID.m_AppId}, result {cb.m_eResult}) -- not one of ours.");
                        return;
                    }
                    pending.Remove(id);

                    if (cb.m_unAppID.m_AppId != LiftoffAppId)
                    {
                        // Same published-file id under a different app is not a thing Steam does,
                        // but if it ever were, treating it as our success would install nothing.
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] DownloadItemResult_t for {id} carried AppID {cb.m_unAppID.m_AppId}, expected {LiftoffAppId}.");
                    }

                    bool ok = cb.m_eResult == Steamworks.EResult.k_EResultOK;
                    string reason = ok ? "" : cb.m_eResult.ToString();
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] DownloadItemResult_t for {id}: {cb.m_eResult} ({(ok ? "ok" : "fail")}) after {(DateTime.UtcNow - req.StartedUtc).TotalSeconds:F1}s.");
                    Complete(id.ToString(CultureInfo.InvariantCulture), ok, reason, req.AnnounceInChat, req.RequestedBy);
                }
                catch (Exception ex)
                {
                    // Never throw back into Steam's callback dispatch.
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] OnDownloadItemResult failed: {ex}");
                }
            }

            // Timeouts + progress logging. Progress is for humans only: GetItemDownloadInfo is
            // NEVER used as a completion signal (rule 3) -- only DownloadItemResult_t is.
            private static void ServicePending()
            {
                if (pending.Count == 0) return;

                List<ulong> timedOut = null;
                DateTime now = DateTime.UtcNow;
                foreach (var entry in pending)
                {
                    PendingDownload req = entry.Value;
                    if ((now - req.StartedUtc).TotalSeconds >= TimeoutSeconds)
                    {
                        if (timedOut == null) timedOut = new List<ulong>();
                        timedOut.Add(entry.Key);
                        continue;
                    }
                    if ((now - req.LastProgressLogUtc).TotalSeconds >= ProgressLogIntervalSeconds)
                    {
                        req.LastProgressLogUtc = now;
                        LogDownloadProgress(req.Id);
                    }
                }

                if (timedOut == null) return;
                foreach (ulong id in timedOut)
                {
                    PendingDownload req = pending[id];
                    pending.Remove(id);
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Workshop download {id} timed out after {TimeoutSeconds:F0}s with no DownloadItemResult_t.");
                    Complete(id.ToString(CultureInfo.InvariantCulture), false, "timeout", req.AnnounceInChat, req.RequestedBy);
                }
            }

            private static void LogDownloadProgress(ulong id)
            {
                try
                {
                    ulong downloaded, total;
                    if (Steamworks.SteamUGC.GetItemDownloadInfo((Steamworks.PublishedFileId_t)id, out downloaded, out total) && total > 0)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop download {id}: {downloaded}/{total} bytes ({(100.0 * downloaded / total):F0}%).");
                    }
                    else
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop download {id}: no progress info yet.");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] GetItemDownloadInfo({id}) failed: {ex.Message}");
                }
            }

            // The one place a result is published: writes the result file (every completed
            // download writes it, whoever started it, so the file is an unambiguous "a workshop
            // item just landed" signal) and, for /dl, posts the follow-up chat line.
            private static void Complete(string idText, bool ok, string reason, bool announceInChat, string requestedBy)
            {
                WriteResultFile(idText, ok, reason);
                LogJsonEvent("workshop_download_result",
                    ("id", idText),
                    ("ok", ok),
                    ("reason", string.IsNullOrEmpty(reason) ? null : reason),
                    ("requested_by", requestedBy));

                if (!announceInChat) return;
                // Queued, not sent directly: the callback can resolve minutes later, while the bot
                // is mid-race or between rooms, where SendChatMessage reliably fails. The queue is
                // flushed from HandleGameRoom once a chat panel exists.
                if (ok)
                {
                    string extra = reason == "already_installed"
                        ? " (it was already installed)"
                        : " — it becomes rotatable at the next track gather.";
                    QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Workshop item {FormatVariable(idText)} downloaded{extra}");
                }
                else
                {
                    QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Workshop download of {FormatVariable(idText)} failed: {FormatVariable(reason)}");
                }
            }

            // '|' is the field separator and the file is one line, so a rejected id that
            // contains either would produce a record the reader cannot parse -- and an
            // unparseable result reads as "not ready yet", i.e. the requester would wait out
            // its whole timeout instead of being told `bad_id`. Only ever reachable through
            // the bad_id path (Steam ids are digits), which is exactly the path that carries
            // unvalidated text.
            private static string SanitizeField(string value)
            {
                if (string.IsNullOrEmpty(value)) return "";
                return value.Replace('|', '_').Replace('\r', '_').Replace('\n', '_');
            }

            private static void WriteResultFile(string idText, bool ok, string reason)
            {
                if (string.IsNullOrEmpty(pluginPath)) return;
                try
                {
                    string line = string.Format(CultureInfo.InvariantCulture, "{0}|{1}|{2}\n",
                                                SanitizeField(idText), ok ? "ok" : "fail",
                                                SanitizeField(reason));
                    File.WriteAllText(Path.Combine(pluginPath, ResultFileName), line);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Wrote {ResultFileName}: {line.Trim()}");
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write {ResultFileName}: {ex.Message}");
                }
            }
        }
    }
}
