using System.Collections.Generic;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Public command — anyone in the lobby may run it. Shows the last few tracks played,
        // most recent first. Backed by the in-memory trackHistory list (capped at 5), which is
        // appended at each settings-popup submit-success point (see CaptureLoadedTrack).
        private class HistoryCommand : IChatCommand
        {
            public string Name => "/history";
            public string Description => "Show the last few tracks played.";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                if (trackHistory.Count == 0)
                {
                    SendTaggedLines("HISTORY", activeTheme.infoTagColor, "No tracks played yet.");
                    return;
                }

                // Last 3, most recent first.
                var recent = new List<string>();
                for (int i = trackHistory.Count - 1; i >= 0 && recent.Count < 3; i--)
                    recent.Add(trackHistory[i]);

                var parts = new List<string>();
                for (int i = 0; i < recent.Count; i++)
                    parts.Add($"{i + 1}. {FormatVariable($"{recent[i]}")}");

                SendTaggedLines("HISTORY", activeTheme.infoTagColor, string.Join(" | ", parts));
            }
        }
    }
}
