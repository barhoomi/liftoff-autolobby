using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Public command — anyone in the lobby may run it. Migrated verbatim from the
        // old HandleChatCommand /info branch (which ran before the admin gate).
        private class InfoCommand : IChatCommand
        {
            public string Name => "/info";
            public string Description => "Show current playlist, rotation timer, next track and room info.";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                string currentPlaylist = "all_official_races";
                string playlistPath = Path.Combine(pluginPath, "playlist_name.txt");
                if (File.Exists(playlistPath))
                    currentPlaylist = File.ReadAllText(playlistPath).Trim();

                double rotationInterval = GetRotationInterval();
                double elapsed = roomCreatedTime != DateTime.MinValue && roomCreatedTime != DateTime.MaxValue
                    ? (DateTime.Now - roomCreatedTime).TotalSeconds : 0;
                double remaining = Math.Max(0, rotationInterval - elapsed);

                string nextEnv, nextMode;
                int trackIdx;
                string nextTrackName = PeekNextTrackName(out nextEnv, out nextMode, out trackIdx);

                string response = $"{FormatTag("INFO", activeTheme.infoTagColor)} Playlist: {FormatVariable($"{currentPlaylist}")} | Interval: {FormatVariable($"{rotationInterval:F0}s")} | Next in: {FormatVariable($"{remaining:F0}s")} | Next: {FormatVariable($"{nextEnv} - {nextTrackName}")} ";
                SendChatMessage(response);

                bool isVisible; string roomName; int maxPlayers; int playerCount;
                if (TryGetRoomInfo(out isVisible, out roomName, out maxPlayers, out playerCount))
                {
                    string visibility = isVisible ? "public" : "private";
                    string ownership = roomOwnedByBot ? "bot-owned" : $"<color={activeTheme.alertTagColor}>NOT bot-owned — settings/rotation unavailable</color>";
                    string roomInfo = $"{FormatTag("INFO", activeTheme.infoTagColor)} Room: {FormatVariable($"{roomName}")} | Visibility: {FormatVariable($"{visibility}")} | Players: {FormatVariable($"{playerCount}/{maxPlayers}")} | {ownership}";
                    SendChatMessage(roomInfo);
                }
            }
        }
    }
}
