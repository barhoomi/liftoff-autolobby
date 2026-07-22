namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // client-lifecycle-commands.md (R4). Admin-only in both roles (IsAdmin is role-aware:
        // admin_ids.txt on server, the local/host player on client — plugin-mode-split.md).
        // Deliberately NO room-ownership requirement, unlike most admin commands: this only
        // writes a settings-source flag (Plugin.Config.cs), which has no Photon side effect of
        // its own to gate on ownership. Keeping the whole /start /stop /pause /resume family on
        // the same simple "just admin" gate makes /stop the always-available escape hatch the
        // spec requires, and an asymmetric gate across the four would be confusing.
        //
        // Scope note (operator amendment, 2026-07-23): originally spec'd client-mode-only,
        // refusing in server mode. Now functional in both roles so a server admin can freeze
        // rotation for a race night instead of abusing /interval — see the feature doc's
        // "Server-mode scope amendment" section.
        private class StartCommand : IChatCommand
        {
            public string Name => "/start";
            public string Description => "Activate track rotation in this room (not the same as auto-starting a race). Usage: /start";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                string env, mode; int idx;
                string next = PeekNextTrackName(out env, out mode, out idx);
                if (string.IsNullOrEmpty(next))
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Can't start rotation — no tracks configured. Add lines to {FormatVariable("tracks_to_rotate.txt")} first.");
                    return;
                }

                Settings.SetRotationEngaged(true);
                Settings.SetRotationPaused(false);
                // Force an immediate track application ("announce the first track") instead of
                // waiting out whatever the rotation timer happens to read right now. Reuses the
                // existing skipRequested lever (Plugin.GameRoom.cs already ORs around the
                // interval check for it) rather than adding a second forced-advance mechanism.
                skipRequested = true;
                chatWarnedAboutNextRace = false;

                SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Rotation started by {FormatVariable(userName)}. Next: {FormatHighlight($"{env} - {next}")}");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} ran /start — rotation engaged.");
            }
        }
    }
}
