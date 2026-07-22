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

            // player-onboarding-ux.md work item 2: role-aware description -- available_playlists
            // .txt is populated by the Python orchestrator (server mode only; see public-
            // release-v1 D3, "v1 player scope = rotation over a hand-edited tracks_to_rotate
            // .txt"), so a client-mode reader would otherwise see a command that sounds like it
            // should work and instead gets an empty "Available: " list with no explanation.
            public string Description => IsServerMode
                ? "Show or set the active playlist. Usage: /playlist [name]"
                : "Server-bot only — v1 client mode has one rotation file, not switchable playlists. Edit tracks_to_rotate.txt directly.";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                // player-onboarding-ux.md work item 5: playlists.json/available_playlists.txt is
                // an orchestrator-only concept (no client install has one) -- say so instead of
                // the generic "Unknown playlist. Available: " (empty) a client-mode admin would
                // otherwise see, which explains nothing. See the Description comment above.
                if (IsClientMode)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Playlists aren't available in client mode — edit {FormatVariable("tracks_to_rotate.txt")} directly.");
                    return;
                }

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
