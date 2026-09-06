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
            //     "already_installed"), else bad_id / download_rejected / queue_full /
            //     <EResult name> / timeout. Consumed (read + deleted) by the control plane.
            //   workshop_unsubscribe_request.txt  written by the control plane after it
            //     quarantines a rejected item (workshop-ingest-hardening.md §1.4): up to 16
            //     decimal ids, one per line. Same one-shot discipline as the request file --
            //     deleted the instant the plugin starts acting on it.
            //   workshop_download_busy.txt     written by the plugin on EVERY tick while any
            //     download is pending or queued (content: the outstanding count), deleted on
            //     the tick that count reaches 0. It is a heartbeat, not a flag: the refreshed
            //     mtime is what lets the auto-ingest tell "still downloading" from "the file
            //     was left behind by a process that died" (workshop-ingest-hardening.md §4.1).
            public const string RequestFileName = "workshop_download_request.txt";
            public const string ResultFileName = "workshop_download_result.txt";
            public const string UnsubscribeRequestFileName = "workshop_unsubscribe_request.txt";
            public const string BusyFileName = "workshop_download_busy.txt";

            // Cap on the ids one workshop_unsubscribe_request.txt may carry -- the control
            // plane writes one line per quarantined batch member, and a batch is at most a
            // handful of items. A file longer than this is junk, not a request.
            public const int MaxUnsubscribeIdsPerRequest = 16;

            // FIFO depth for /dl submissions waiting on the in-flight download. Small on
            // purpose: the queue exists to make `/dl <track_id> <race_id>` work, not to be a
            // job scheduler, and each entry can cost up to TimeoutSeconds.
            public const int MaxQueuedDownloads = 8;

            // Matches the game's own PopupShareContent.RoutineCheckItemUpdateProgress budget
            // (120s, found in the spike's decompile) rather than inventing a new number.
            public const double TimeoutSeconds = 120.0;

            // Liftoff's Steam AppID, confirmed in the decompile via
            // SteamUGC.CreateItem((AppId_t)410340u, ...) -- spike Q3/Q5.
            private const uint LiftoffAppId = 410340u;

            private const double ProgressLogIntervalSeconds = 15.0;

            // How long to hold a queued download back while a previous outcome is still
            // sitting unconsumed in the single-slot result file. The control plane polls it
            // every 1-2s, so this only ever elapses when nothing is reading at all (client
            // mode, or a stopped orchestrator) -- in which case the wait would be pointless
            // and we proceed rather than stall the queue forever.
            private const double ResultHandoffTimeoutSeconds = 15.0;

            // Long-lived, never null once registered: see the class comment (finalizer unregisters).
            private static Steamworks.Callback<Steamworks.DownloadItemResult_t> downloadResultCallback;

            // Same lifetime rule as downloadResultCallback -- a CallResult whose only
            // reference is a local would be finalized (and so unregistered) before Steam
            // answers. Reusing ONE CallResult across requests is acceptable here and only
            // here: a CallResult tracks a single call handle, and since downloads are started
            // only from the tick -- at most one per tick, and never while a previous outcome
            // is unread (see ResultSlotFree) -- two subscribes are at least a second apart,
            // far longer than a Steam call result takes. The failure mode if one ever did
            // overlap is a dropped LOG LINE, not a dropped download: nothing branches on this
            // result, which is why it is safe to trade the extra CallResult for the
            // simplicity. What a failed subscribe actually costs surfaces downstream as
            // game_listing_missing.
            private static Steamworks.CallResult<Steamworks.RemoteStorageSubscribePublishedFileResult_t> subscribeResultCallResult;

            // When the current wait for a consumer to take the result file started; MinValue
            // while the slot is free. See ResultSlotFree().
            private static DateTime resultWaitSinceUtc = DateTime.MinValue;

            // The id the in-flight SubscribeItem belongs to, recorded at Set() time. Taken
            // from here rather than from a field of the result struct deliberately: we
            // already know the id, and this keeps the log line independent of the pinned
            // SDK's exact RemoteStorageSubscribePublishedFileResult_t layout.
            private static ulong subscribeInFlightId;

            // Confirms SteamUGC.UnsubscribeItem landed (workshop-ingest-hardening.md §1.4).
            // Nothing branches on it -- it exists so a quarantine that failed to detach the
            // item from the bot account is visible in the log instead of being silent.
            private static Steamworks.Callback<Steamworks.RemoteStoragePublishedFileUnsubscribed_t> unsubscribedCallback;

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

            // Submissions waiting for the in-flight download to resolve. Only /dl fills this
            // (the file protocol stays strictly one id at a time -- PollRequestFile refuses to
            // read while anything is pending OR queued), which is what keeps the single result
            // file a one-outcome-at-a-time channel.
            private static readonly Queue<QueuedDownload> queued = new Queue<QueuedDownload>();

            private class QueuedDownload
            {
                public string IdText;
                public bool AnnounceInChat;
                public string RequestedBy;
            }

            // Called once per second from RunTick (Plugin.cs). Cheap when idle: two File.Exists
            // (the request file and the busy marker) plus one for the unsubscribe request.
            public static void Tick()
            {
                try
                {
                    PollRequestFile();
                    PollUnsubscribeRequestFile();
                    ServicePending();
                    UpdateBusyMarker();
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] WorkshopDownloader.Tick failed: {ex}");
                }
            }

            /// <summary>
            /// Submit a download. ALWAYS queues; the tick is the only thing that ever starts
            /// one. This is what /dl calls; the file protocol still goes through
            /// PollRequestFile -> TryStartDownload directly, one id at a time.
            /// </summary>
            /// <remarks>
            /// There is deliberately no "nothing pending, start it right now" fast path.
            /// TryStartDownload can resolve SYNCHRONOUSLY (bad_id / already_installed /
            /// download_rejected), so with a fast path a two-id /dl produced two Complete()
            /// calls in a single frame: the second WriteResultFile overwrote the first in the
            /// single-slot result file before any consumer could read it, the busy heartbeat
            /// never got written at all (pending+queued was 0 at every tick boundary), and the
            /// second SubscribeItem's Set() unregistered the first call handle. Queueing
            /// unconditionally makes "at most one outcome published per tick" true by
            /// construction. The cost is that a /dl starts up to one tick (1s) later.
            /// </remarks>
            public static void EnqueueDownload(string id, bool announceInChat, string requestedBy)
            {
                int ahead = pending.Count + queued.Count;
                if (queued.Count >= MaxQueuedDownloads)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Refusing to queue workshop download of '{id}': {queued.Count} already queued (cap {MaxQueuedDownloads}).");
                    Complete(id == null ? "" : id.Trim(), false, "queue_full", announceInChat, requestedBy);
                    return;
                }

                queued.Enqueue(new QueuedDownload
                {
                    IdText = id,
                    AnnounceInChat = announceInChat,
                    RequestedBy = requestedBy,
                });
                if (ahead > 0)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop download of {id} queued behind {ahead} other(s).");
                }
                else
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop download of {id} queued -- starting on the next tick.");
                }
            }

            // True when the single-slot result file is free to receive the next outcome.
            //
            // workshop_download_result.txt holds ONE record. Starting a download whose
            // outcome may land (or resolve synchronously) while the previous outcome is still
            // unread would overwrite that outcome, and a lost result means a download nobody
            // ever ingests -- while chat has already said "downloaded, ingesting now".
            // Waiting for the consumer to take it is the only thing that actually prevents
            // that; the bounded give-up keeps an install with no control plane (client mode)
            // from wedging its own queue.
            private static bool ResultSlotFree()
            {
                if (string.IsNullOrEmpty(pluginPath)) return true;

                bool present;
                try { present = File.Exists(Path.Combine(pluginPath, ResultFileName)); }
                catch (Exception) { return true; }

                if (!present)
                {
                    resultWaitSinceUtc = DateTime.MinValue;
                    return true;
                }
                if (resultWaitSinceUtc == DateTime.MinValue)
                {
                    resultWaitSinceUtc = DateTime.UtcNow;
                    return false;
                }
                if ((DateTime.UtcNow - resultWaitSinceUtc).TotalSeconds < ResultHandoffTimeoutSeconds)
                {
                    return false;
                }

                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Nothing consumed {ResultFileName} within {ResultHandoffTimeoutSeconds:F0}s -- proceeding anyway; the earlier outcome will be overwritten. Is the orchestrator running?");
                resultWaitSinceUtc = DateTime.MinValue;
                return true;
            }

            // The batch-boundary heartbeat the auto-ingest reads (§4.1). Rewritten every tick
            // while anything is outstanding so a reader can distinguish "still working" from
            // "a crashed process left this behind"; deleted as soon as nothing is outstanding,
            // which on the first idle tick after launch also clears a marker a previous
            // process died holding.
            private static void UpdateBusyMarker()
            {
                if (string.IsNullOrEmpty(pluginPath)) return;
                string path = Path.Combine(pluginPath, BusyFileName);
                int outstanding = pending.Count + queued.Count;
                try
                {
                    if (outstanding > 0)
                    {
                        File.WriteAllText(path, outstanding.ToString(CultureInfo.InvariantCulture) + "\n");
                    }
                    else if (File.Exists(path))
                    {
                        File.Delete(path);
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to update {BusyFileName}: {ex.Message}");
                }
            }

            private static void PollRequestFile()
            {
                if (string.IsNullOrEmpty(pluginPath)) return;

                // One at a time: while a download is in flight -- or a /dl batch is still
                // queued behind it -- the request file is left untouched, so a second request
                // simply waits its turn instead of clobbering the single result file. A file
                // request never jumps a queued chat batch (§4.1); the CLI's own bound may trip
                // meanwhile, and its watcher_timeout text says exactly that.
                if (pending.Count > 0 || queued.Count > 0) return;

                // Nor while a previous outcome is still unread: this request may resolve
                // synchronously (bad_id / already_installed) and would overwrite it.
                if (!ResultSlotFree()) return;

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

            // workshop-ingest-hardening.md §1.4: the control plane quarantines a rejected item
            // and THEN asks for it to be unsubscribed. Unsubscribing is what stops Steam
            // silently re-downloading the item back into the content root (and what keeps it
            // out of SteamUGC.GetSubscribedItems, i.e. out of the next availability sweep).
            // Same read -> delete-before-acting -> act discipline as PollRequestFile: an
            // unsubscribe replayed forever after a crash would be worse than one missed.
            private static void PollUnsubscribeRequestFile()
            {
                if (string.IsNullOrEmpty(pluginPath)) return;

                string requestPath = Path.Combine(pluginPath, UnsubscribeRequestFileName);
                if (!File.Exists(requestPath)) return;

                string[] lines;
                try
                {
                    lines = File.ReadAllLines(requestPath);
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to read {UnsubscribeRequestFileName}: {ex.Message}");
                    return; // transient read error (e.g. mid-replace): retry on the next tick
                }

                try { File.Delete(requestPath); }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to delete {UnsubscribeRequestFileName}: {ex.Message} -- refusing to process it, it would be reprocessed forever.");
                    return;
                }

                EnsureCallbacksRegistered();

                int handled = 0;
                foreach (string line in lines)
                {
                    if (handled >= MaxUnsubscribeIdsPerRequest)
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] {UnsubscribeRequestFileName} carried more than {MaxUnsubscribeIdsPerRequest} ids -- ignoring the rest.");
                        break;
                    }
                    string text = (line ?? "").Trim();
                    if (text.Length == 0) continue;

                    ulong raw;
                    if (!ulong.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out raw) || raw == 0)
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Ignoring '{text}' in {UnsubscribeRequestFileName}: not a published-file id.");
                        continue;
                    }

                    handled++;
                    Steamworks.SteamUGC.UnsubscribeItem((Steamworks.PublishedFileId_t)raw);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] SteamUGC.UnsubscribeItem({raw}) issued.");
                }
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

                // Register BEFORE any Steam call: a result callback can in principle arrive
                // before the call that triggered it returns, and a late registration would
                // miss it.
                EnsureCallbacksRegistered();

                // Subscribe FIRST, unconditionally, before any state inspection
                // (workshop-ingest-hardening.md §1.1). The game enumerates the content it will
                // offer from SteamUGC.GetSubscribedItems (spike Q5, decompile-confirmed), not
                // from what happens to be on disk -- so a DownloadItem-only fetch lands files
                // the game never lists, which is exactly the failure this fixes. Subscribing is
                // idempotent for an already-subscribed item, so there is no "should I?" branch.
                // The returned SteamAPICall_t is a handle, NOT a success signal (AGENTS.md rule
                // 2/3): nothing branches on it, and a failed subscription surfaces downstream as
                // the control plane's game_listing_missing, which is the check that matters.
                subscribeInFlightId = raw;
                Steamworks.SteamAPICall_t call = Steamworks.SteamUGC.SubscribeItem(id);
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] SteamUGC.SubscribeItem({raw}) issued (call={call.m_SteamAPICall}).");
                if (subscribeResultCallResult != null) subscribeResultCallResult.Set(call);

                // Already on disk? Mirrors the game's own state check (spike Q2/Q4) and avoids a
                // redundant round-trip for an item subscribed in a previous session. "Installed"
                // is Steam's manifest talking, and a manifest survives the quarantine
                // shutil.move that took the files away (reproduced live 2026-09-03), so the
                // folder must actually be there before this short-circuits to success -- see
                // workshop-ingest-hardening.md §6.
                uint state = Steamworks.SteamUGC.GetItemState(id);
                bool installed = (state & (uint)Steamworks.EItemState.k_EItemStateInstalled) != 0;
                bool needsUpdate = (state & (uint)Steamworks.EItemState.k_EItemStateNeedsUpdate) != 0;
                if (installed && !needsUpdate)
                {
                    ulong sizeOnDisk; string folder; uint timeStamp;
                    bool haveInfo = Steamworks.SteamUGC.GetItemInstallInfo(id, out sizeOnDisk, out folder, 1024u, out timeStamp);
                    bool onDisk = haveInfo && !string.IsNullOrEmpty(folder) && System.IO.Directory.Exists(folder);
                    if (onDisk)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Workshop item {raw} is already installed (state=0x{state:X}) -- nothing to download.");
                        Complete(raw.ToString(CultureInfo.InvariantCulture), true, "already_installed", announceInChat, requestedBy);
                        return false;
                    }
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Workshop item {raw} reports installed (state=0x{state:X}) but its folder is missing (folder='{folder}') — downloading again.");
                }

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

            private static void EnsureCallbacksRegistered()
            {
                if (downloadResultCallback == null)
                {
                    downloadResultCallback = Steamworks.Callback<Steamworks.DownloadItemResult_t>.Create(OnDownloadItemResult);
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Registered Callback<DownloadItemResult_t> (kept in a static field so the finalizer can't unregister it).");
                }
                if (subscribeResultCallResult == null)
                {
                    subscribeResultCallResult = Steamworks.CallResult<Steamworks.RemoteStorageSubscribePublishedFileResult_t>.Create(OnSubscribeItemResult);
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Registered CallResult<RemoteStorageSubscribePublishedFileResult_t> (evidence only -- nothing branches on it).");
                }
                if (unsubscribedCallback == null)
                {
                    unsubscribedCallback = Steamworks.Callback<Steamworks.RemoteStoragePublishedFileUnsubscribed_t>.Create(OnPublishedFileUnsubscribed);
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Registered Callback<RemoteStoragePublishedFileUnsubscribed_t>.");
                }
            }

            // Evidence only: a subscription failure is NOT a download failure, and this never
            // writes the result file, fails the download, or gates anything. What a failed
            // subscribe actually costs shows up downstream as the control plane's
            // game_listing_missing -- the check that reflects the property we care about
            // ("does the game list this track"), rather than the call that was supposed to
            // cause it.
            private static void OnSubscribeItemResult(Steamworks.RemoteStorageSubscribePublishedFileResult_t cb, bool ioFailure)
            {
                try
                {
                    string idText = subscribeInFlightId.ToString(CultureInfo.InvariantCulture);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] SubscribeItem result for {idText}: {cb.m_eResult} (ioFailure={ioFailure})");
                    LogJsonEvent("workshop_subscribe_result",
                        ("id", idText),
                        ("result", cb.m_eResult.ToString()));
                }
                catch (Exception ex)
                {
                    // Never throw back into Steam's callback dispatch.
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] OnSubscribeItemResult failed: {ex}");
                }
            }

            private static void OnPublishedFileUnsubscribed(Steamworks.RemoteStoragePublishedFileUnsubscribed_t cb)
            {
                try
                {
                    ulong id = cb.m_nPublishedFileId.m_PublishedFileId;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Unsubscribed {id} (app {cb.m_nAppID.m_AppId}).");
                    LogJsonEvent("workshop_unsubscribed", ("id", id.ToString(CultureInfo.InvariantCulture)));
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] OnPublishedFileUnsubscribed failed: {ex}");
                }
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
                ExpireAndLogPending();

                // One at a time, one per tick, and only once the previous outcome has been
                // taken: the result file carries a single outcome, so this is what keeps a
                // reader from ever seeing two of them collapse into one file.
                if (pending.Count == 0 && queued.Count > 0 && ResultSlotFree())
                {
                    QueuedDownload next = queued.Dequeue();
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Starting queued workshop download of {next.IdText} ({queued.Count} still queued).");
                    TryStartDownload(next.IdText, next.AnnounceInChat, next.RequestedBy);
                }
            }

            private static void ExpireAndLogPending()
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

                // A landed item is only *playable* once the game's own Environment x GameMode
                // sweep has seen it, and that sweep is cached across rotations -- so every
                // success re-arms it (workshop-ingest-hardening.md §2.4). already_installed
                // included: that path now (re-)subscribes too, and the game may never have
                // listed the item before.
                if (ok) SweepRefresh.Arm($"workshop_download:{idText}");

                if (!announceInChat) return;
                // Queued, not sent directly: the callback can resolve minutes later, while the bot
                // is mid-race or between rooms, where SendChatMessage reliably fails. The queue is
                // flushed from HandleGameRoom once a chat panel exists.
                if (ok)
                {
                    string extra = reason == "already_installed"
                        ? " (it was already installed; re-subscribed)"
                        : " — ingesting now; it becomes rotatable after the next availability sweep.";
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
