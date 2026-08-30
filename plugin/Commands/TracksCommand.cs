using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;

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
        // Lists tracks from the current playlist, sorted alphabetically, paginated.
        private class TracksCommand : IChatCommand
        {
            private const int PageSize = 5;

            public string Name => "/tracks";
            public string Description => "List tracks matching keyword or all tracks, paginated. Usage: /tracks [keyword] [page]";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                    if (!File.Exists(tracksPath))
                    {
                        SendTaggedLines("TRACKS", activeTheme.infoTagColor, new string[] { "No tracks available in the rotation list." });
                        return;
                    }

                    // ReadStaticTracks (Plugin.Rotation.cs) is the SAME parser the rotation
                    // engine indexes against, so PlaylistIndex here always means exactly the
                    // same position /track expects (bug-shuffle-toggle-and-tracks-
                    // incompatibility.md, Option 2) -- one parser, not two independently
                    // maintained copies that could quietly drift apart on an edge case.
                    List<string> staticTracks = ReadStaticTracks(tracksPath);
                    var allTracks = new List<SearchResult>();
                    for (int i = 0; i < staticTracks.Count; i++)
                    {
                        // Rightmost-split (bug-comma-in-track-name.md) -- same parser the
                        // rotation engine uses, so a track name containing a comma (e.g.
                        // "Iceberg, Right ahead!") never shears its own fields.
                        string trackName, environment, gameMode;
                        ParseTrackLine(staticTracks[i], out trackName, out environment, out gameMode);
                        if (string.IsNullOrEmpty(environment)) environment = "The Drawing Board";
                        if (string.IsNullOrEmpty(gameMode)) gameMode = "Classic Race";

                        allTracks.Add(new SearchResult
                        {
                            PlaylistIndex = i,
                            TrackName = trackName,
                            Environment = environment,
                            GameMode = gameMode
                        });
                    }

                    if (allTracks.Count == 0)
                    {
                        SendTaggedLines("TRACKS", activeTheme.infoTagColor, new string[] { "No valid tracks found in the playlist." });
                        return;
                    }

                    // Parse arguments: extract optional page number from the end of the argument string
                    string keyword = argument.Trim();
                    int page = 1;

                    string[] argParts = keyword.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    if (argParts.Length > 1)
                    {
                        int parsedPage;
                        if (int.TryParse(argParts[argParts.Length - 1], out parsedPage) && parsedPage > 0)
                        {
                            page = parsedPage;
                            keyword = string.Join(" ", argParts, 0, argParts.Length - 1).Trim();
                        }
                    }
                    else if (argParts.Length == 1)
                    {
                        int parsedPage;
                        if (int.TryParse(argParts[0], out parsedPage) && parsedPage > 0)
                        {
                            page = parsedPage;
                            keyword = "";
                        }
                    }

                    // Find matches
                    var matches = new List<SearchResult>();
                    if (string.IsNullOrEmpty(keyword))
                    {
                        matches.AddRange(allTracks);
                    }
                    else
                    {
                        foreach (var track in allTracks)
                        {
                            if (track.TrackName.IndexOf(keyword, StringComparison.OrdinalIgnoreCase) >= 0 ||
                                track.Environment.IndexOf(keyword, StringComparison.OrdinalIgnoreCase) >= 0)
                            {
                                matches.Add(track);
                            }
                        }
                    }

                    if (matches.Count == 0)
                    {
                        SendTaggedLines("TRACKS", activeTheme.infoTagColor, new string[] { $"No tracks found matching keyword: {FormatVariable(keyword)}" });
                        return;
                    }

                    // Sort matches alphabetically: by Environment name first, then by TrackName
                    matches.Sort((a, b) =>
                    {
                        int envCompare = string.Compare(a.Environment, b.Environment, StringComparison.OrdinalIgnoreCase);
                        if (envCompare != 0) return envCompare;
                        return string.Compare(a.TrackName, b.TrackName, StringComparison.OrdinalIgnoreCase);
                    });

                    // Paginate
                    int totalPages = Mathf.CeilToInt((float)matches.Count / PageSize);
                    if (page < 1) page = 1;
                    if (page > totalPages) page = totalPages;

                    var pageItems = new List<SearchResult>();
                    int startIndex = (page - 1) * PageSize;
                    for (int i = startIndex; i < Math.Min(startIndex + PageSize, matches.Count); i++)
                    {
                        pageItems.Add(matches[i]);
                    }

                    // Save the page results in static memory for the subsequent /track command selection
                    lastSearchResults.Clear();
                    lastSearchResults.AddRange(pageItems);

                    // Build multi-line response
                    var linesList = new List<string>();
                    string header = $"Matches (Page {page}/{totalPages})";
                    if (!string.IsNullOrEmpty(keyword))
                    {
                        header += $" for keyword: {FormatVariable(keyword)}";
                    }
                    linesList.Add(header);

                    for (int i = 0; i < pageItems.Count; i++)
                    {
                        linesList.Add($"{i + 1}. {FormatVariable(pageItems[i].TrackName)} ({pageItems[i].Environment})");
                    }

                    SendTaggedLines("TRACKS", activeTheme.infoTagColor, linesList.ToArray());
                }
                catch (Exception ex)
                {
                    SendTaggedLines("TRACKS", activeTheme.infoTagColor, new string[] { $"Error searching tracks: {ex.Message}" });
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in /tracks: {ex}");
                }
            }
        }
    }
}
