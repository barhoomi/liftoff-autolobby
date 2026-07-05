namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /skip case.
        private class SkipCommand : IChatCommand
        {
            public string Name => "/skip";
            public string Description => "Skip to the next track.";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                skipRequested = true;
                chatWarnedAboutNextRace = false;
                SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Skipping to next track.");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /skip");
            }
        }
    }
}
