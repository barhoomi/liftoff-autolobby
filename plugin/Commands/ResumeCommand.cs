namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // client-lifecycle-commands.md (R4). See StartCommand.cs for the admin-only /
        // no-ownership-gate rationale shared by this whole command family.
        //
        // Unfreezes the timer from wherever MaintainRotationPauseFreeze (Plugin.cs) held it —
        // "remaining time preserved" falls out of that mechanism for free: elapsed simply
        // continues growing from the frozen value the instant this stops holding it there.
        private class ResumeCommand : IChatCommand
        {
            public string Name => "/resume";
            public string Description => "Unfreeze the rotation timer from where it was paused. Usage: /resume";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                Settings.SetRotationPaused(false);
                if (IsClientMode)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Rotation timer resumed.");
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Rotation timer resumed by {FormatVariable(userName)}.");
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} ran /resume.");
            }
        }
    }
}
