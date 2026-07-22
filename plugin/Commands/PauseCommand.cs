namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // client-lifecycle-commands.md (R4). See StartCommand.cs for the admin-only /
        // no-ownership-gate rationale shared by this whole command family.
        //
        // Freezes only the AUTOMATIC rotation timer (Plugin.cs's MaintainRotationPauseFreeze,
        // driven off IsRotationPaused()). /skip, /track, and democracy skip-votes are untouched
        // by this feature and keep working while paused — they force an advance via
        // skipRequested, which Plugin.GameRoom.cs's trigger condition ORs around the interval
        // check unconditionally, independent of this pause.
        private class PauseCommand : IChatCommand
        {
            public string Name => "/pause";
            public string Description => "Freeze the rotation timer (current track stays loaded; /skip and /track still work). Usage: /pause";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                Settings.SetRotationPaused(true);
                string note = IsRotationEngaged() ? "" : " (rotation is currently stopped — /start to activate)";
                SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Rotation timer paused by {FormatVariable(userName)}.{note}");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} ran /pause.");
            }
        }
    }
}
