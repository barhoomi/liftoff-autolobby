using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /interval case.
        private class IntervalCommand : IChatCommand
        {
            public string Name => "/interval";
            public string Description => "Set rotation interval. Usage: /interval <seconds> (min 30)";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                double newInterval;
                if (double.TryParse(argument, out newInterval) && newInterval >= 30.0)
                {
                    File.WriteAllText(Path.Combine(pluginPath, "rotation_interval.txt"), newInterval.ToString("F0"));
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Interval set to {newInterval:F0}s.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set interval to {newInterval}s");
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /interval <seconds> (min 30)");
                }
            }
        }
    }
}
