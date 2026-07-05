using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /public case.
        private class PublicCommand : IChatCommand
        {
            public string Name => "/public";
            public string Description => "Make the room public (visible in the lobby list).";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                string curName;
                string setErr;
                if (SetRoomVisibility(false, out curName, out setErr))
                {
                    try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "false"); } catch { }
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room is now public.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set room to public.");
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Could not change visibility: {setErr}.");
                }
            }
        }
    }
}
