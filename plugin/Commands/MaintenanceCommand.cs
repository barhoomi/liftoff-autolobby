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

            // player-onboarding-ux.md work item 2: role-aware description -- a client-mode
            // reader should not see "Schedule a shutdown" and reasonably assume it applies to
            // them. The server-mode wording is unchanged.
            public string Description => IsServerMode
                ? "Schedule a shutdown. Usage: /maintenance [minutes|cancel]"
                : "Server-bot only — does not apply in client mode (never closes your own game).";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                // player-onboarding-ux.md work item 5: maintenance means a scheduled
                // Application.Quit() of the SERVER's own process (plugin-mode-split.md's gate
                // table: "Whole maintenance block is server-only" -- RunServerMaintenanceTick,
                // where the quit actually fires, only ever runs under IsServerMode). In client
                // mode that would mean quitting the requesting player's own game out from under
                // them, which the plugin must never do -- so refuse with the real reason instead
                // of writing maintenance_active.txt and claiming a shutdown that will never fire.
                // That "claims success, does nothing" shape is exactly what AGENTS.md rule 2 (the
                // /kick no-op bug) warns against. CanExecute is intentionally left unchanged
                // (still just IsAdmin) so this refusal — not CommandRegistry's generic "this bot
                // does not own the room" message — is what a client-mode admin sees.
                if (IsClientMode)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} /maintenance is a server-bot feature and has no effect here — it will never close your own game.");
                    return;
                }

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
