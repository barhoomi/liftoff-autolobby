using System;
using System.Collections.Generic;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-only + room-ownership required. Rotates to the previous track.
        private class PrevCommand : IChatCommand
        {
            public string Name => "/prev";
            public string Description => "Rotate to the previous track.";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                    int count = CountValidRotationLines();
                    if (count <= 0)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Cannot rotate: no tracks in rotation list.");
                        return;
                    }

                    int index = 0;
                    if (File.Exists(statePath))
                    {
                        int.TryParse(File.ReadAllText(statePath).Trim(), out index);
                    }

                    // The next index that will be loaded is current index.
                    // Decrementing by 2 points the next loaded track to the previous one in the rotation cycle.
                    int prevIndex = (index - 2) % count;
                    if (prevIndex < 0) prevIndex += count;

                    File.WriteAllText(statePath, prevIndex.ToString());

                    skipRequested = true;
                    chatWarnedAboutNextRace = false;

                    // client-chat-presentation.md: server-mode wording is the exact original
                    // literal (byte-identical -- it never named the target track). Client mode
                    // is enriched to resolve and show it: prevIndex is a WALK position (an index
                    // into GetActiveRotationOrder's permutation, same scheme PeekNextTrackName
                    // and /track use), so translate walk position -> static index -> the actual
                    // tracks_to_rotate.txt line, same lookup TrackCommand already does.
                    if (IsClientMode)
                    {
                        string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                        List<string> validTracks = ReadStaticTracks(tracksPath);
                        List<int> activeOrder = GetActiveRotationOrder(validTracks, forceReshuffle: false);
                        string prevTrackName = "", prevEnv = "";
                        if (prevIndex >= 0 && prevIndex < activeOrder.Count)
                        {
                            int staticIdx = activeOrder[prevIndex];
                            if (staticIdx >= 0 && staticIdx < validTracks.Count)
                            {
                                // Rightmost-split (bug-comma-in-track-name.md).
                                string prevMode;
                                ParseTrackLine(validTracks[staticIdx], out prevTrackName, out prevEnv, out prevMode);
                            }
                        }
                        string body = RenderClientTemplate(Settings.PrevTrackTemplate, "Rotating to the previous track: {track} ({environment})",
                            ("track", prevTrackName), ("environment", prevEnv));
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, body);
                    }
                    else
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Rotating to the previous track.");
                    }
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /prev. Updated state index from {index} to {prevIndex}");
                }
                catch (Exception ex)
                {
                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Error executing /prev: {ex.Message}");
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error executing /prev: {ex.Message}");
                }
            }
        }
    }
}
