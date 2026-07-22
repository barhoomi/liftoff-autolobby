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
                        // Capture "what's coming up next" under the still-active shuffled order
                        // BEFORE flipping shuffleMode, so turning shuffle off doesn't yank the
                        // already-announced "Up next" track out from under anyone -- the least-
                        // surprising choice (bug-shuffle-toggle-and-tracks-incompatibility.md,
                        // Option 2): only the ordering going FORWARD changes; the very next pick
                        // stays the same. Definition order resumes sequentially from there.
                        string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                        string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                        var validTracks = ReadStaticTracks(tracksPath);
                        if (validTracks.Count > 0 && shuffleMode)
                        {
                            List<int> activeOrder = GetActiveRotationOrder(validTracks, forceReshuffle: false);
                            int cursor = 0;
                            if (File.Exists(statePath)) int.TryParse(File.ReadAllText(statePath).Trim(), out cursor);
                            if (cursor < 0 || cursor >= activeOrder.Count) cursor = 0;
                            int staticIndex = activeOrder[cursor];
                            // Identity order means cursor == static index, so this single write
                            // both preserves the next pick and re-anchors the cursor to definition
                            // order for every pick after it.
                            File.WriteAllText(statePath, staticIndex.ToString());
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
