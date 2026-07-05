using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin + room-ownership. Migrated verbatim from the old /mode case.
        private class ModeCommand : IChatCommand
        {
            public string Name => "/mode";
            public string Description => "Show or set the game mode. Usage: /mode infinite|circuit|dropout|survival|auto";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                if (string.IsNullOrEmpty(argument))
                {
                    string currentMode = GetOverrideGameMode();
                    if (string.IsNullOrEmpty(currentMode)) currentMode = "auto (playlist default)";
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Current mode: {FormatVariable($"{currentMode}")}. Usage: /mode infinite|circuit|dropout|survival|auto");
                }
                else
                {
                    string targetMode = "";
                    string lowerArg = argument.Trim().ToLower();
                    if (lowerArg == "infinite") targetMode = "Infinite Race";
                    else if (lowerArg == "circuit" || lowerArg == "classic") targetMode = "Classic Race";
                    else if (lowerArg == "dropout") targetMode = "Dropout Race";
                    else if (lowerArg == "survival") targetMode = "Survival";
                    else if (lowerArg == "auto" || lowerArg == "off" || lowerArg == "reset") targetMode = "auto";

                    if (targetMode == "")
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Invalid mode. Supported: infinite, circuit, dropout, survival, auto");
                    }
                    else if (targetMode == "auto")
                    {
                        try
                        {
                            string path = Path.Combine(pluginPath, "override_game_mode.txt");
                            if (File.Exists(path)) File.Delete(path);
                            SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Game mode reset to {FormatVariable($"playlist default")}.");
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} reset override game mode to auto.");
                        }
                        catch (Exception ex)
                        {
                            UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to delete override_game_mode.txt: {ex.Message}");
                        }
                    }
                    else
                    {
                        try
                        {
                            File.WriteAllText(Path.Combine(pluginPath, "override_game_mode.txt"), targetMode);
                            SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Game mode set to {FormatVariable($"{targetMode}")}.");
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set override game mode to {targetMode}.");
                        }
                        catch (Exception ex)
                        {
                            UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write override_game_mode.txt: {ex.Message}");
                        }
                    }
                }
            }
        }
    }
}
