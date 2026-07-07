using System;
using System.Collections.Generic;
using System.Reflection;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-only + room-ownership required. Promotes another player to room host.
        private class PromoteCommand : IChatCommand
        {
            public string Name => "/promote";
            public string Description => "Promote a player to lobby host. Usage: /promote <username>";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    string targetName = argument.Trim();
                    if (string.IsNullOrEmpty(targetName))
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Usage: /promote <username>");
                        return;
                    }

                    Type networkType = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                                       Type.GetType("PhotonNetwork, Assembly-CSharp");
                    if (networkType == null)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Promotion failed: PhotonNetwork class not found.");
                        return;
                    }

                    PropertyInfo playerListProp = networkType.GetProperty("PlayerList", BindingFlags.Public | BindingFlags.Static);
                    if (playerListProp == null)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Promotion failed: PlayerList property not found.");
                        return;
                    }

                    Array playerArray = (Array)playerListProp.GetValue(null);
                    if (playerArray == null || playerArray.Length == 0)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Promotion failed: Player list is empty.");
                        return;
                    }

                    var matches = new List<object>();
                    var matchNames = new List<string>();

                    for (int i = 0; i < playerArray.Length; i++)
                    {
                        object playerObj = playerArray.GetValue(i);
                        if (playerObj == null) continue;

                        PropertyInfo nickProp = playerObj.GetType().GetProperty("NickName") ?? playerObj.GetType().GetProperty("Nickname");
                        if (nickProp == null) continue;

                        string nick = (string)nickProp.GetValue(playerObj, null) ?? "";
                        
                        if (nick.IndexOf(targetName, StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            PropertyInfo localProp = playerObj.GetType().GetProperty("IsLocal");
                            bool isLocal = false;
                            if (localProp != null) isLocal = (bool)localProp.GetValue(playerObj, null);
                            if (isLocal) continue; // Do not promote local bot to itself

                            matches.Add(playerObj);
                            matchNames.Add(nick);
                        }
                    }

                    if (matches.Count == 0)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"No players found matching target: {FormatVariable(targetName)}");
                        return;
                    }

                    if (matches.Count > 1)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Multiple players match target: {FormatVariable(string.Join(", ", matchNames.ToArray()))}");
                        return;
                    }

                    // Perform promotion via PhotonNetwork.SetMasterClient(player)
                    MethodInfo setMasterMethod = networkType.GetMethod("SetMasterClient", BindingFlags.Public | BindingFlags.Static, null, new[] { matches[0].GetType() }, null);
                    if (setMasterMethod == null)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, "Promotion failed: SetMasterClient method not found.");
                        return;
                    }

                    object result = setMasterMethod.Invoke(null, new[] { matches[0] });
                    if (result is bool success && success)
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Promoting {FormatVariable(matchNames[0])} to room owner.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /promote for {matchNames[0]} successfully.");
                    }
                    else
                    {
                        SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Failed to promote {FormatVariable(matchNames[0])} (Photon set host rejected).");
                    }
                }
                catch (Exception ex)
                {
                    SendTaggedLines("ADMIN", activeTheme.adminTagColor, $"Error executing /promote: {ex.Message}");
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception in /promote: {ex}");
                }
            }
        }
    }
}
