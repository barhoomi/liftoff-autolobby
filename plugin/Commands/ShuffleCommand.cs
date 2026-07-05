using System;
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
                    // Deal a fresh order right now rather than waiting for the current
                    // pass to finish, so "on" visibly takes effect immediately.
                    ShuffleTracksFile(Path.Combine(pluginPath, "tracks_to_rotate.txt"));
                    File.WriteAllText(Path.Combine(pluginPath, "rotation_state.txt"), "0");
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shuffle on.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} enabled shuffle");
                }
                else if (argument.Equals("off", StringComparison.OrdinalIgnoreCase))
                {
                    shuffleMode = false;
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "shuffle_mode.txt"), "false");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write shuffle_mode.txt: {ex.Message}");
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
