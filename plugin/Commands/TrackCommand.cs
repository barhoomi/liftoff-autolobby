using System;
using System.Collections.Generic;
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

                    // selected.PlaylistIndex is a position in the STATIC tracks_to_rotate.txt
                    // (bug-shuffle-toggle-and-tracks-incompatibility.md, Option 2) -- stable
                    // regardless of shuffle mode, since the plugin never rewrites that file.
                    // Translate it into a WALK position (a position in the active rotation
                    // order) so the very next pick is this exact track whether or not shuffle
                    // is currently on, then continue the same walk from there afterward.
                    var validTracks = ReadStaticTracks(Path.Combine(pluginPath, "tracks_to_rotate.txt"));
                    if (selected.PlaylistIndex < 0 || selected.PlaylistIndex >= validTracks.Count)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "That track is no longer in tracks_to_rotate.txt (the playlist changed since /tracks ran) -- run /tracks again.");
                        return;
                    }

                    List<int> activeOrder = GetActiveRotationOrder(validTracks, forceReshuffle: false);
                    int walkPos = activeOrder.IndexOf(selected.PlaylistIndex);
                    if (walkPos < 0) walkPos = selected.PlaylistIndex; // defensive only -- activeOrder is always a full permutation of every valid index

                    string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                    File.WriteAllText(statePath, walkPos.ToString());

                    skipRequested = true;
                    chatWarnedAboutNextRace = false;

                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Rotating to track: {FormatVariable(selected.TrackName)} ({selected.Environment})");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /track {index}. Updated rotation cursor (walk position {walkPos}) to static index {selected.PlaylistIndex}");
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
