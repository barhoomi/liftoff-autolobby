using System;
using System.Collections.Generic;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // /dl <workshop_id> [<workshop_id> ...] -- docs/features/doing/workshop-ingame-download.md
        // and docs/features/doing/workshop-ingest-hardening.md §4.2.
        //
        // A thin wrapper over WorkshopDownloader.EnqueueDownload: acknowledge in chat
        // immediately, then let the tick post each outcome when the Steamworks callback resolves
        // (or the 120s timeout fires). The chat handler NEVER blocks waiting on Steam -- the
        // whole point of the callback design is that the download is asynchronous.
        //
        // Multiple ids because Liftoff publishes a track and its race as SEPARATE workshop
        // items: a one-id ingest deadlocks (the track alone fails NO_MATCHING_RACE, the race
        // alone fails its track dependency and gets quarantined, after which the track can never
        // validate). The ids download one at a time through the existing single-id protocol --
        // the queue lives in WorkshopDownloader, not in the request/result files.
        //
        // Admin-only, but deliberately NOT room-ownership-gated (unlike /maxplayers, /private
        // and friends): downloading workshop content changes nothing about the room, so an
        // admin can prime a track while the bot is a guest in someone else's lobby.
        private class DlCommand : IChatCommand
        {
            // A batch is validated as a SET, and a set larger than this is not a track/race
            // pair any more -- it is a bulk import, which belongs in the CLI where its outcome
            // can be read line by line.
            private const int MaxIdsPerCommand = 4;

            public string Name => "/dl";
            public string Description => "Download Steam Workshop tracks by id. Usage: /dl <workshop_id> [<workshop_id> …] (max 4)";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                // Echoed back into chat below, so strip anything tag-shaped first: a '<' in a
                // bot message corrupts SplitMessage's tag tracking (the same constraint
                // trackcheck's NAME_UNSAFE_MARKUP check exists for). A real workshop id is
                // digits; anything else is on its way to a `bad_id` result anyway. A token that
                // is EMPTY once stripped (someone typed the angle brackets literally) is
                // dropped rather than submitted: an empty id would make the plugin write a
                // result line with no id, which the control plane cannot parse and would
                // therefore wait out in full.
                string[] tokens = (argument ?? "").Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                var ids = new List<string>();
                foreach (string token in tokens)
                {
                    string id = token.Trim().Replace("<", "").Replace(">", "");
                    if (id.Length > 0) ids.Add(id);
                }

                if (ids.Count == 0)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /dl <workshop_id> [<workshop_id> …] (the numeric ids from the Steam Workshop URLs)");
                    return;
                }
                if (ids.Count > MaxIdsPerCommand)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /dl <workshop_id> [<workshop_id> …] (max {MaxIdsPerCommand})");
                    return;
                }

                string idList = string.Join(", ", ids.ToArray());

                // Acknowledge ONCE for the whole batch: for a real download the follow-up can be
                // up to 120s away per id, and silence in between reads as "the command did
                // nothing". Each id still gets its own outcome line from Complete(), so there is
                // exactly one place that formats a result (AGENTS.md rule 4).
                SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Queued workshop download of {FormatVariable(idList)} — I'll report back when Steam answers.");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} requested workshop download of '{idList}' via /dl.");

                foreach (string id in ids)
                {
                    WorkshopDownloader.EnqueueDownload(id, announceInChat: true, requestedBy: userName);
                }
            }
        }
    }
}
