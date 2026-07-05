using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /maxplayers case.
        private class MaxPlayersCommand : IChatCommand
        {
            public string Name => "/maxplayers";
            public string Description => "Set the room player limit. Usage: /maxplayers <number> (min 2)";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                int requestedMax;
                if (!int.TryParse(argument, out requestedMax) || requestedMax < 2)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /maxplayers <number> (min 2)");
                }
                else
                {
                    int applied;
                    string setErr;
                    if (SetRoomMaxPlayers(requestedMax, out applied, out setErr))
                    {
                        try { File.WriteAllText(Path.Combine(pluginPath, "max_players.txt"), applied.ToString()); } catch { }
                        string clampNote = applied != requestedMax ? $" (clamped from {requestedMax})" : "";
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Max players set to {FormatVariable($"{applied}")}{clampNote}.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set max players to {applied} (requested {requestedMax}).");
                    }
                    else
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Could not set max players: {setErr}.");
                    }
                }
            }
        }
    }
}
