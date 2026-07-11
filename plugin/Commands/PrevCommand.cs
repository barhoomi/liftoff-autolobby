using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-only + room-ownership required. Rotates to the previous track.
        private class PrevCommand : IChatCommand
        {
            public string Name => "/prev";
            public string Description => "Rotate to the previous track.";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                    int count = CountValidRotationLines();
                    if (count <= 0)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Cannot rotate: no tracks in rotation list.");
                        return;
                    }

                    int index = 0;
                    if (File.Exists(statePath))
                    {
                        int.TryParse(File.ReadAllText(statePath).Trim(), out index);
                    }

                    // The next index that will be loaded is current index.
                    // Decrementing by 2 points the next loaded track to the previous one in the rotation cycle.
                    int prevIndex = (index - 2) % count;
                    if (prevIndex < 0) prevIndex += count;

                    File.WriteAllText(statePath, prevIndex.ToString());

                    skipRequested = true;
                    chatWarnedAboutNextRace = false;

                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Rotating to the previous track.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /prev. Updated state index from {index} to {prevIndex}");
                }
                catch (Exception ex)
                {
                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Error executing /prev: {ex.Message}");
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error executing /prev: {ex.Message}");
                }
            }
        }
    }
}
