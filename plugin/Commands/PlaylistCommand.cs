using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /playlist case.
        private class PlaylistCommand : IChatCommand
        {
            public string Name => "/playlist";
            public string Description => "Show or set the active playlist. Usage: /playlist [name]";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                if (string.IsNullOrEmpty(argument))
                {
                    string current = "";
                    string playlistPath = Path.Combine(pluginPath, "playlist_name.txt");
                    if (File.Exists(playlistPath)) current = File.ReadAllText(playlistPath).Trim();
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Current playlist: {FormatVariable($"{current}")}. Available: {FormatVariable($"{GetAvailablePlaylistsString()}")}");
                }
                else if (PlaylistExists(argument))
                {
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "playlist_name.txt"), argument.Trim());
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Playlist set to {FormatVariable($"{argument.Trim()}")}. Next track will be from the new playlist.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set playlist to {argument}");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write playlist_name.txt: {ex.Message}");
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Failed to change playlist due to internal error.");
                    }
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Unknown playlist. Available: {FormatVariable($"{GetAvailablePlaylistsString()}")}");
                }
            }
        }
    }
}
