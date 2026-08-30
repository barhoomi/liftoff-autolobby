namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // /dl <workshop_id> -- docs/features/doing/workshop-ingame-download.md.
        //
        // A thin wrapper over WorkshopDownloader.TryStartDownload: acknowledge in chat
        // immediately, then let the tick post the outcome when the Steamworks callback resolves
        // (or the 120s timeout fires). The chat handler NEVER blocks waiting on Steam -- the
        // whole point of the callback design is that the download is asynchronous.
        //
        // Admin-only, but deliberately NOT room-ownership-gated (unlike /maxplayers, /private
        // and friends): downloading workshop content changes nothing about the room, so an
        // admin can prime a track while the bot is a guest in someone else's lobby.
        private class DlCommand : IChatCommand
        {
            public string Name => "/dl";
            public string Description => "Download a Steam Workshop track by id. Usage: /dl <workshop_id>";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                // Echoed back into chat below, so strip anything tag-shaped first: a '<' in a
                // bot message corrupts SplitMessage's tag tracking (the same constraint
                // trackcheck's NAME_UNSAFE_MARKUP check exists for). A real workshop id is
                // digits; anything else is on its way to a `bad_id` result anyway.
                string id = (argument ?? "").Trim().Replace("<", "").Replace(">", "");
                if (id.Length == 0)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /dl <workshop_id> (the numeric id from the Steam Workshop URL)");
                    return;
                }

                // Acknowledge first: for a real download the follow-up can be up to 120s away,
                // and silence in between reads as "the command did nothing".
                SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Queued workshop download of {FormatVariable(id)} — I'll report back when Steam answers.");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} requested workshop download of '{id}' via /dl.");

                // Returns false when it already resolved (bad id / already installed / Steam
                // refused it); either way the outcome message is posted by WorkshopDownloader,
                // so there is exactly one place that formats a result (AGENTS.md rule 4).
                WorkshopDownloader.TryStartDownload(id, announceInChat: true, requestedBy: userName);
            }
        }
    }
}
