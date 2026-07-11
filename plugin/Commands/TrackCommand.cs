using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-only + room-ownership required. Sets the next track to one of the search results and skips.
        private class TrackCommand : IChatCommand
        {
            public string Name => "/track";
            public string Description => "Load a track by search result index (1-5). Usage: /track <number>";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    if (lastSearchResults.Count == 0)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "No recent search results. Run /tracks [keyword] first.");
                        return;
                    }

                    int index;
                    if (!int.TryParse(argument.Trim(), out index))
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Invalid argument. Usage: /track <1-5>");
                        return;
                    }

                    if (index < 1 || index > lastSearchResults.Count)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Invalid index. Please specify a number between 1 and {lastSearchResults.Count}.");
                        return;
                    }

                    SearchResult selected = lastSearchResults[index - 1];

                    string statePath = Path.Combine(pluginPath, "rotation_state.txt");

                    // Set rotation_state.txt to the chosen track index
                    File.WriteAllText(statePath, selected.PlaylistIndex.ToString());

                    skipRequested = true;
                    chatWarnedAboutNextRace = false;

                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Rotating to track: {FormatVariable(selected.TrackName)} ({selected.Environment})");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /track {index}. Updated state index to {selected.PlaylistIndex}");
                }
                catch (Exception ex)
                {
                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Error executing /track: {ex.Message}");
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error executing /track: {ex.Message}");
                }
            }
        }
    }
}
