using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. New command per docs/features/doing/democracy-skip.md.
        // Toggles whether /skip is a public majority vote (see SkipCommand).
        private class DemocracyCommand : IChatCommand
        {
            public string Name => "/democracy";
            public string Description => "Toggle democracy mode (public /skip voting). Usage: /democracy on|off";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                if (argument.Equals("on", StringComparison.OrdinalIgnoreCase))
                {
                    democracyEnabled = true;
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "democracy_mode.txt"), "true");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write democracy_mode.txt: {ex.Message}");
                    }
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Democracy mode enabled. Players can now vote to skip tracks with {FormatVariable("/skip")}.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} enabled democracy mode");
                }
                else if (argument.Equals("off", StringComparison.OrdinalIgnoreCase))
                {
                    democracyEnabled = false;
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "democracy_mode.txt"), "false");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write democracy_mode.txt: {ex.Message}");
                    }
                    // Votes cast under democracy mode have no meaning once it's turned off.
                    skipVotes.Clear();
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Democracy mode disabled. Skip is now admin-only.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} disabled democracy mode");
                }
                else
                {
                    string currentState = democracyEnabled ? "on" : "off";
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Democracy mode is currently {FormatVariable(currentState)}. Usage: /democracy on|off");
                }
            }
        }
    }
}
