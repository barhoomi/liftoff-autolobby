namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Public command -- anyone in the lobby may run it. New in build-release-pipeline.md
        // (R1 item 3): every future bug report from a stranger starts with "what version are
        // you on", so this has to exist before the plugin ships to the public. Reads the same
        // PluginVersion.Number constant stamped into the [BepInPlugin] attribute in Plugin.cs,
        // which is itself generated from the single <Version> in Directory.Build.props -- so
        // there is exactly one place that has to be bumped per release.
        private class VersionCommand : IChatCommand
        {
            public string Name => "/version";
            public string Description => "Show the plugin version.";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                SendChatMessage($"{FormatTag("INFO", activeTheme.infoTagColor)} LiftoffAutoLobby {FormatVariable(PluginVersion.Number)}");
            }
        }
    }
}
