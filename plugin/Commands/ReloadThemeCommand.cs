namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-only. Migrated verbatim from the old /reloadtheme case.
        // NOTE: not a room-ownership command (it was NOT in the old requiresOwnership
        // list), so CanExecute gates on admin status only.
        private class ReloadThemeCommand : IChatCommand
        {
            public string Name => "/reloadtheme";
            public string Description => "Reload the chat color theme from chat_theme.json.";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                bool themeLoaded = LoadThemeConfig();
                if (themeLoaded)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Chat theme reloaded successfully.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} reloaded chat theme.");
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Failed to load chat theme configuration (invalid JSON). Defaults applied.");
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Admin {userName} attempted /reloadtheme but chat_theme.json was invalid; defaults applied.");
                }
            }
        }
    }
}
