namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // client-lifecycle-commands.md (R4). See StartCommand.cs for the admin-only /
        // no-ownership-gate rationale shared by this whole command family.
        //
        // "/stop ... Must always work, even from a half-broken state" (the spec's own words) is
        // why CanExecute has no other precondition beyond IsAdmin: this is the plugin's off
        // switch for rotation, and it must not itself be gateable into uselessness.
        private class StopCommand : IChatCommand
        {
            public string Name => "/stop";
            public string Description => "Deactivate rotation — return to idle. Current track stays loaded. Usage: /stop";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                Settings.SetRotationEngaged(false);
                Settings.SetRotationPaused(false); // stop is a full reset, not a resumable freeze
                SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Rotation stopped by {FormatVariable(userName)}. Current track stays loaded — run /start to resume automatic rotation.");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} ran /stop — rotation disengaged.");
            }
        }
    }
}
