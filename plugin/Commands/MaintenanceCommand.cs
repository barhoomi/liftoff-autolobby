using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-only. Migrated verbatim from the old /maintenance case.
        // NOTE: not a room-ownership command (it was NOT in the old requiresOwnership
        // list), so CanExecute gates on admin status only.
        private class MaintenanceCommand : IChatCommand
        {
            public string Name => "/maintenance";
            public string Description => "Schedule a shutdown. Usage: /maintenance [minutes|cancel]";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                if (!string.IsNullOrEmpty(argument) && argument.Equals("cancel", StringComparison.OrdinalIgnoreCase))
                {
                    CancelMaintenance();
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Scheduled maintenance cancelled.");
                }
                else
                {
                    double mins = 5.0;
                    if (!string.IsNullOrEmpty(argument))
                    {
                        double.TryParse(argument, out mins);
                    }
                    if (mins <= 0) mins = 5.0;

                    maintenanceActive = true;
                    maintenanceTime = DateTime.Now.AddMinutes(mins);
                    lastMaintenanceWarningMinutes = -1;
                    maintenanceWarning30sSent = false;
                    maintenanceWarning10sSent = false;
                    try
                    {
                        File.WriteAllText(Path.Combine(pluginPath, "maintenance_active.txt"), "true");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write maintenance_active.txt: {ex.Message}");
                    }
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Shutdown for maintenance scheduled in {FormatVariable($"{mins:F1}m")}.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} scheduled maintenance in {mins} minutes.");
                }
            }
        }
    }
}
