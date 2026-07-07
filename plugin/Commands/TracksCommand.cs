using System;
using System.Collections.Generic;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        private struct SearchResult
        {
            public int PlaylistIndex;
            public string TrackName;
            public string Environment;
            public string GameMode;
        }

        private static readonly List<SearchResult> lastSearchResults = new List<SearchResult>();

        // Public command — anyone in the lobby may run it.
        // Lists up to 5 tracks from the current playlist that match the keyword.
        private class TracksCommand : IChatCommand
        {
            public string Name => "/tracks";
            public string Description => "List tracks matching keyword or next 5 upcoming.";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                    if (!File.Exists(tracksPath))
                    {
                        SendTaggedLines("TRACKS", activeTheme.infoTagColor, "No tracks available in the rotation list.");
                        return;
                    }

                    string[] lines = File.ReadAllLines(tracksPath);
                    var allTracks = new List<SearchResult>();
                    int playlistIndex = 0;
                    for (int i = 0; i < lines.Length; i++)
                    {
                        string line = lines[i].Trim();
                        if (string.IsNullOrEmpty(line) || line.StartsWith("#")) continue;

                        string[] parts = line.Split(',');
                        string trackName = parts[0].Trim();
                        string environment = parts.Length > 1 ? parts[1].Trim() : "The Drawing Board";
                        string gameMode = parts.Length > 2 ? parts[2].Trim() : "Classic Race";

                        allTracks.Add(new SearchResult
                        {
                            PlaylistIndex = playlistIndex,
                            TrackName = trackName,
                            Environment = environment,
                            GameMode = gameMode
                        });
                        playlistIndex++;
                    }

                    if (allTracks.Count == 0)
                    {
                        SendTaggedLines("TRACKS", activeTheme.infoTagColor, "No valid tracks found in the playlist.");
                        return;
                    }

                    string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                    int currentIndex = 0;
                    if (File.Exists(statePath))
                    {
                        int.TryParse(File.ReadAllText(statePath).Trim(), out currentIndex);
                    }
                    if (currentIndex < 0 || currentIndex >= allTracks.Count)
                    {
                        currentIndex = 0;
                    }

                    var matches = new List<SearchResult>();
                    string keyword = argument.Trim();

                    if (string.IsNullOrEmpty(keyword))
                    {
                        // Take next 5 upcoming tracks starting from currentIndex
                        for (int i = 0; i < Math.Min(5, allTracks.Count); i++)
                        {
                            int targetIndex = (currentIndex + i) % allTracks.Count;
                            matches.Add(allTracks[targetIndex]);
                        }
                    }
                    else
                    {
                        // Search for keyword in TrackName or Environment, starting from currentIndex for closest matches
                        for (int i = 0; i < allTracks.Count; i++)
                        {
                            int targetIndex = (currentIndex + i) % allTracks.Count;
                            SearchResult candidate = allTracks[targetIndex];
                            if (candidate.TrackName.IndexOf(keyword, StringComparison.OrdinalIgnoreCase) >= 0 ||
                                candidate.Environment.IndexOf(keyword, StringComparison.OrdinalIgnoreCase) >= 0)
                            {
                                matches.Add(candidate);
                                if (matches.Count >= 5) break;
                            }
                        }
                    }

                    if (matches.Count == 0)
                    {
                        SendTaggedLines("TRACKS", activeTheme.infoTagColor, $"No tracks found matching keyword: {FormatVariable(keyword)}");
                        return;
                    }

                    // Save search results in static memory for the subsequent /track command
                    lastSearchResults.Clear();
                    lastSearchResults.AddRange(matches);

                    var partsList = new List<string>();
                    for (int i = 0; i < matches.Count; i++)
                    {
                        partsList.Add(FormatVariable($"{i + 1}.{matches[i].TrackName} ({matches[i].Environment})"));
                    }

                    SendTaggedLines("TRACKS", activeTheme.infoTagColor, string.Join(" | ", partsList.ToArray()));
                }
                catch (Exception ex)
                {
                    SendTaggedLines("TRACKS", activeTheme.infoTagColor, $"Error searching tracks: {ex.Message}");
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in /tracks: {ex}");
                }
            }
        }
    }
}
