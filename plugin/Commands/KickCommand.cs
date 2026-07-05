namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated VERBATIM from the old /kick case.
        //
        // NOTE: /kick is KNOWN-BROKEN and its fix is DEFERRED — see
        // docs/features/backlog/kick-command-broken.md. This migration preserves the
        // current (non-working) behavior exactly and intentionally does NOT attempt a
        // fix. Do not treat KickPlayer's return as a reliable "check the reflected bool"
        // exemplar; use HandleMasterClientSwitched as the confirmation-pattern reference.
        private class KickCommand : IChatCommand
        {
            public string Name => "/kick";
            public string Description => "Kick a player from the room. Usage: /kick <player_name>";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                if (string.IsNullOrEmpty(argument))
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /kick <player_name>");
                }
                else
                {
                    string matchedName;
                    string matchesList;
                    if (KickPlayer(argument, out matchedName, out matchesList))
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Kicked player {FormatVariable($"{matchedName}")}.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} kicked player {matchedName}");
                    }
                    else if (matchedName == "multiple")
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Multiple matches found: {FormatVariable($"{matchesList}")}. Please be more specific.");
                    }
                    else
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} No player found matching {FormatVariable($"'{argument}'")}.");
                    }
                }
            }
        }
    }
}
