using System;
using System.Collections.Generic;
using System.Reflection;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Public command — anyone may run it.
        // Lists all active players in the room with their Photon ActorNumber.
        private class PlayersCommand : IChatCommand
        {
            public string Name => "/players";
            public string Description => "List all active players in the lobby and their Photon IDs.";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                try
                {
                    Type networkType = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                                       Type.GetType("PhotonNetwork, Assembly-CSharp");
                    if (networkType == null)
                    {
                        SendTaggedLines("PLAYERS", activeTheme.infoTagColor, new string[] { "Failed to resolve PhotonNetwork." });
                        return;
                    }

                    PropertyInfo playerListProp = networkType.GetProperty("PlayerList", BindingFlags.Public | BindingFlags.Static);
                    if (playerListProp == null)
                    {
                        SendTaggedLines("PLAYERS", activeTheme.infoTagColor, new string[] { "Failed to resolve PlayerList property." });
                        return;
                    }

                    Array playerArray = (Array)playerListProp.GetValue(null);
                    if (playerArray == null || playerArray.Length == 0)
                    {
                        SendTaggedLines("PLAYERS", activeTheme.infoTagColor, new string[] { "No players found in room." });
                        return;
                    }

                    var linesList = new List<string>();
                    linesList.Add($"Active players in room ({playerArray.Length}):");

                    for (int i = 0; i < playerArray.Length; i++)
                    {
                        object playerObj = playerArray.GetValue(i);
                        if (playerObj == null) continue;

                        Type playerType = playerObj.GetType();
                        PropertyInfo nickProp = playerType.GetProperty("NickName") ?? playerType.GetProperty("Nickname");
                        string nick = nickProp != null ? nickProp.GetValue(playerObj, null) as string : "Unknown";

                        PropertyInfo actorProp = playerType.GetProperty("ActorNumber");
                        int actorNumber = -1;
                        if (actorProp != null)
                        {
                            actorNumber = (int)actorProp.GetValue(playerObj, null);
                        }

                        FieldInfo localField = playerType.GetField("IsLocal", BindingFlags.Public | BindingFlags.Instance);
                        bool isLocal = localField != null && (bool)localField.GetValue(playerObj);

                        string localSuffix = isLocal ? (IsClientMode ? " [you]" : " [bot]") : "";
                        linesList.Add($"{i + 1}. {FormatVariable(nick)} (ID: {FormatVariable(actorNumber.ToString())}){localSuffix}");
                    }

                    SendTaggedLines("PLAYERS", activeTheme.infoTagColor, linesList.ToArray());
                }
                catch (Exception ex)
                {
                    SendTaggedLines("PLAYERS", activeTheme.infoTagColor, new string[] { $"Error retrieving player list: {ex.Message}" });
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in /players: {ex}");
                }
            }
        }
    }
}
