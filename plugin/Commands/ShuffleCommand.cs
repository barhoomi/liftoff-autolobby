using System;
using System.Collections.Generic;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /shuffle case.
        private class ShuffleCommand : IChatCommand
        {
            public string Name => "/shuffle";
            public string Description => "Toggle track shuffle. Usage: /shuffle on|off";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                if (argument.Equals("on", StringComparison.OrdinalIgnoreCase))
                {
                    shuffleMode = true;
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "shuffle_mode.txt"), "true");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write shuffle_mode.txt: {ex.Message}");
                    }
                    // Deal a fresh order right now rather than waiting for the current pass to
                    // finish, so "on" visibly takes effect immediately. tracks_to_rotate.txt
                    // itself is never rewritten (bug-shuffle-toggle-and-tracks-incompatibility.md,
                    // Option 2) -- the deal is persisted separately in shuffle_order.txt.
                    var validTracks = ReadStaticTracks(Path.Combine(pluginPath, "tracks_to_rotate.txt"));
                    GetActiveRotationOrder(validTracks, forceReshuffle: true);
                    File.WriteAllText(Path.Combine(pluginPath, "rotation_state.txt"), "0");
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shuffle on.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} enabled shuffle");
                }
                else if (argument.Equals("off", StringComparison.OrdinalIgnoreCase))
                {
                    try
                    {
                        // Operator ruling (2026-07-23 live session -- amends the earlier
                        // "preserve the already-announced next pick" design): after /shuffle
                        // off the NEXT track played must be the DEFINITION-ORDER SUCCESSOR of
                        // the CURRENT track, NOT the pre-committed shuffled up-next pick. See
                        // bug-shuffle-toggle-and-tracks-incompatibility.md, "Operator ruling".
                        //
                        // rotation_state.txt already points one PAST the current track (it was
                        // advanced to the up-next pick when the current track was selected), so
                        // the fix anchors to the CURRENT track's own static index and steps +1.
                        // lastRotationIndex is the static index of the most recently loaded
                        // track (set at every selection in GetNextTrackFromRotationOnce, and it
                        // reflects /track selections too, which load through that same method).
                        // Fall back to activeOrder[cursor-1] only when lastRotationIndex is out
                        // of range (e.g. -1 right after a game-process relaunch, before this
                        // session's first rotation). Under the now-identity order cursor ==
                        // static index, so writing (currentStaticIndex + 1) makes the next pick
                        // the definition-order successor and every pick after it ascending
                        // definition order (wrapping cleanly back to line 1).
                        string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                        string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                        var validTracks = ReadStaticTracks(tracksPath);
                        if (validTracks.Count > 0 && shuffleMode)
                        {
                            int n = validTracks.Count;
                            List<int> activeOrder = GetActiveRotationOrder(validTracks, forceReshuffle: false);
                            int cursor = 0;
                            if (File.Exists(statePath)) int.TryParse(File.ReadAllText(statePath).Trim(), out cursor);
                            if (cursor < 0 || cursor >= activeOrder.Count) cursor = 0;

                            int currentStaticIndex;
                            if (lastRotationIndex >= 0 && lastRotationIndex < n)
                                currentStaticIndex = lastRotationIndex;
                            else
                                currentStaticIndex = activeOrder[(cursor - 1 + activeOrder.Count) % activeOrder.Count];

                            int newCursor = (currentStaticIndex + 1) % n;
                            File.WriteAllText(statePath, newCursor.ToString());
                        }
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to preserve rotation position while disabling shuffle: {ex.Message}");
                    }

                    shuffleMode = false;
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "shuffle_mode.txt"), "false");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write shuffle_mode.txt: {ex.Message}");
                    }
                    // Defense in depth: reads already ignore shuffle_order.txt while shuffleMode
                    // is false, but deleting it means a later /shuffle on can never accidentally
                    // resurrect this stale deal (it always forces a fresh one anyway -- this just
                    // avoids a confusing leftover file).
                    try
                    {
                        File.Delete(Path.Combine(pluginPath, "shuffle_order.txt"));
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to delete shuffle_order.txt: {ex.Message}");
                    }
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shuffle off.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} disabled shuffle");
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /shuffle on|off");
                }
            }
        }
    }
}
