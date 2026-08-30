using System;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /extend case.
        private class ExtendCommand : IChatCommand
        {
            public string Name => "/extend";
            public string Description => "Extend the current track timer. Usage: /extend <seconds>";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                double extendSecs;
                if (double.TryParse(argument, out extendSecs) && extendSecs > 0)
                {
                    if (roomCreatedTime != DateTime.MinValue && roomCreatedTime != DateTime.MaxValue)
                    {
                        roomCreatedTime = roomCreatedTime.AddSeconds(extendSecs);
                        double newRemaining = Math.Max(0, GetRotationInterval() - (DateTime.Now - roomCreatedTime).TotalSeconds);
                        chatWarnedAboutNextRace = false;
                        if (IsClientMode)
                        {
                            string rendered = RenderClientTemplate(Settings.ExtendTemplate, "Extended by {seconds}s. Next rotation in {time}s.",
                                ("seconds", $"{extendSecs:F0}"), ("time", $"{newRemaining:F0}"),
                                ("track", currentTrackName), ("environment", currentEnvironment));
                            SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} {rendered}");
                        }
                        else
                        {
                            SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Extended by {extendSecs:F0}s. Next rotation in {newRemaining:F0}s.");
                        }
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} extended timer by {extendSecs}s");
                    }
                    else
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} No active rotation timer.");
                    }
                }
                else
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Usage: /extend <seconds>");
                }
            }
        }
    }
}
