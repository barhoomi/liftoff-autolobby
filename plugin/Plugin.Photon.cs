using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Reflection;
using System.Collections.Generic;
using System.Linq;
using BepInEx;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;
using Liftoff.Multiplayer;
using Liftoff.Multiplayer.GameSetup;
using Photon.Realtime;
using ExitGames.Client.Photon;
using HarmonyLib;

namespace LiftoffAutoLobby
{
    // MODE: shared
    // Photon reflection accessors and mutators (PhotonNetwork/room state via
    //     Type.GetType reflection, per the decompile-don't-guess rule in AGENTS.md):
    //     kick, visibility/max-players, room info, leave, master-client-switch handling,
    //     connection-ready checks, and the stuck-auth diagnostic log.
    public partial class AutoLobbyPlugin
    {


        // Sets PhotonNetwork.NickName once the Photon assembly is resolvable. Retried from
        // RunTick() (not called from Awake) because Photon's static classes aren't reliably
        // loaded that early in BepInEx's boot sequence — same reasoning as the other reflective
        // PhotonNetwork accessors below, which are also called per-tick rather than once.
        private static void ApplyBotNicknameIfNeeded()
        {
            // DANGER gate (plugin-mode-split.md): never rename a real player. Client mode must
            // never touch PhotonNetwork.NickName. (botNickname is also never loaded in client
            // mode, so this is belt-and-suspenders.)
            if (IsClientMode) return;
            if (nicknameApplied || string.IsNullOrEmpty(botNickname)) return;
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type != null)
                {
                    PropertyInfo prop = type.GetProperty("NickName", BindingFlags.Public | BindingFlags.Static);
                    if (prop != null && prop.CanWrite)
                    {
                        prop.SetValue(null, botNickname);
                        nicknameApplied = true;
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Applied bot nickname: '{botNickname}'");
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to apply bot nickname: {ex.Message}");
            }
        }

        // Reflects PhotonNetwork.LocalPlayer.UserId — the same value space as the chat user ids
        // delivered to ChatMessagePatch — so client-mode admin resolution (IsLocalPlayer) can
        // compare like-for-like. Cached once non-empty; empty until Photon has assigned a local
        // player (i.e. once connected/in a room).
        private static string cachedLocalPhotonUserId = "";
        private static string GetLocalPhotonUserId()
        {
            if (!string.IsNullOrEmpty(cachedLocalPhotonUserId)) return cachedLocalPhotonUserId;
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type != null)
                {
                    PropertyInfo localPlayerProp = type.GetProperty("LocalPlayer", BindingFlags.Public | BindingFlags.Static);
                    object localPlayer = localPlayerProp?.GetValue(null);
                    if (localPlayer != null)
                    {
                        PropertyInfo userIdProp = localPlayer.GetType().GetProperty("UserId", BindingFlags.Public | BindingFlags.Instance);
                        string uid = userIdProp?.GetValue(localPlayer) as string;
                        if (!string.IsNullOrEmpty(uid)) cachedLocalPhotonUserId = uid;
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] GetLocalPhotonUserId failed: {ex.Message}");
            }
            return cachedLocalPhotonUserId;
        }

        private static void LogPhotonAuthDiagnostics()
        {
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type == null)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] [Diag] PhotonNetwork type not resolvable.");
                    return;
                }
                PropertyInfo stateProp = type.GetProperty("NetworkClientState", BindingFlags.Public | BindingFlags.Static);
                object state = stateProp?.GetValue(null);
                bool isConnected = GetPhotonBoolProperty("IsConnected");
                bool isConnectedAndReady = GetPhotonBoolProperty("IsConnectedAndReady");
                bool inRoom = GetPhotonBoolProperty("InRoom");
                bool inLobby = GetPhotonBoolProperty("InLobby");
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] [Diag] Photon state at stuck-auth error: NetworkClientState={state}, IsConnected={isConnected}, IsConnectedAndReady={isConnectedAndReady}, InRoom={inRoom}, InLobby={inLobby}");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] [Diag] LogPhotonAuthDiagnostics failed: {ex.Message}");
            }
        }

        private static bool KickPlayer(string targetName, out string matchedName, out string matchesList)
        {
            matchedName = "";
            matchesList = "";
            try
            {
                Type networkType = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                                   Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (networkType == null) return false;

                PropertyInfo playerListProp = networkType.GetProperty("PlayerList", BindingFlags.Public | BindingFlags.Static);
                if (playerListProp == null) return false;

                Array playerArray = (Array)playerListProp.GetValue(null);
                if (playerArray == null || playerArray.Length == 0) return false;

                var matches = new List<object>();
                var matchNames = new List<string>();
                int targetActorId;
                bool isNumericId = int.TryParse(targetName, out targetActorId);

                for (int i = 0; i < playerArray.Length; i++)
                {
                    object playerObj = playerArray.GetValue(i);
                    if (playerObj == null) continue;

                    PropertyInfo nickProp = playerObj.GetType().GetProperty("NickName") ?? playerObj.GetType().GetProperty("Nickname");
                    if (nickProp == null) continue;

                    string nick = (string)nickProp.GetValue(playerObj, null) ?? "";
                    
                    bool isMatch = false;
                    if (isNumericId)
                    {
                        PropertyInfo actorProp = playerObj.GetType().GetProperty("ActorNumber");
                        if (actorProp != null && (int)actorProp.GetValue(playerObj, null) == targetActorId)
                        {
                            isMatch = true;
                        }
                    }
                    else
                    {
                        if (nick.IndexOf(targetName, StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            isMatch = true;
                        }
                    }

                    if (isMatch)
                    {
                        PropertyInfo localProp = playerObj.GetType().GetProperty("IsLocal");
                        bool isLocal = false;
                        if (localProp != null) isLocal = (bool)localProp.GetValue(playerObj, null);
                        if (isLocal) continue;

                        matches.Add(playerObj);
                        matchNames.Add(nick);
                    }
                }

                if (matches.Count == 0)
                {
                    return false;
                }
                if (matches.Count == 1)
                {
                    object targetPlayerObj = matches[0];
                    matchedName = matchNames[0];

                    // Find the Room Controller component containing the RPCKicked method
                    UnityEngine.Component targetViewComponent = null;
                    Type customPlayerType = null;
                    UnityEngine.Component[] allComponents = UnityEngine.Object.FindObjectsOfType<UnityEngine.Component>();
                    foreach (var comp in allComponents)
                    {
                        if (comp == null) continue;
                        MethodInfo method = comp.GetType().GetMethod("RPCKicked", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                        if (method != null)
                        {
                            ParameterInfo[] pars = method.GetParameters();
                            if (pars.Length >= 2)
                            {
                                customPlayerType = pars[0].ParameterType;
                                targetViewComponent = comp;
                                break;
                            }
                        }
                    }

                    if (targetViewComponent == null || customPlayerType == null)
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] Kick failed: Could not find Room Controller with RPCKicked method.");
                        return false;
                    }

                    // Find the PhotonView on the Room Controller GameObject
                    UnityEngine.Component targetView = targetViewComponent.GetComponent("Photon.Pun.PhotonView") ?? targetViewComponent.GetComponent("PhotonView");
                    if (targetView == null)
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] Kick failed: Could not find PhotonView on Room Controller.");
                        return false;
                    }

                    // Get local player
                    PropertyInfo localPlayerProp = networkType.GetProperty("LocalPlayer", BindingFlags.Public | BindingFlags.Static);
                    if (localPlayerProp == null) return false;
                    object localPlayerObj = localPlayerProp.GetValue(null);

                    // Construct fresh custom player wrapper instances. Decompiling the wrapper type (via
                    // `ilspycmd -t`) confirmed it is a stateless wrapper: its only constructor is
                    // `public <Wrapper>(Photon.Realtime.Player player)`, which just stores the reference,
                    // and every property (ActorNumber, NickName, IsLocal, IsMasterClient, CustomProperties,
                    // PlayerPlatformInfo -> IsModerator, etc.) is computed live from that stored reference on
                    // each access via a `GetCustomProperty<T>(key)` helper reading `player.CustomProperties`.
                    // There is no persistent registry of "live" wrapper instances anywhere reachable from the
                    // Room Controller (confirmed by a full-assembly reflection scan for any static/instance
                    // field or property that is this type, or a collection of it) -- the previous
                    // GetCustomPlayerForPhotonPlayer/ScanCollectionForCustomPlayer/MatchPlayerObject reflection
                    // scan was searching for something that doesn't exist, which is why it always returned
                    // null. Constructing fresh via the public ctor is both correct and simpler: RPCKicked's
                    // own authorization check reads `senderWrapper.IsMasterClient` / `.PlatformInfo.IsModerator`,
                    // both computed live from the wrapped Photon.Realtime.Player, so a freshly-built wrapper
                    // around PhotonNetwork.LocalPlayer authorizes exactly the same as any "found" instance would.
                    object targetCustomPlayer = null;
                    object localCustomPlayer = null;
                    try
                    {
                        targetCustomPlayer = Activator.CreateInstance(customPlayerType, targetPlayerObj);
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Kick failed: could not construct target Player wrapper: {ex}");
                    }
                    try
                    {
                        localCustomPlayer = Activator.CreateInstance(customPlayerType, localPlayerObj);
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Kick failed: could not construct local (bot) Player wrapper: {ex}");
                    }

                    if (targetCustomPlayer == null || localCustomPlayer == null)
                    {
                        UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Kick failed: could not construct custom Player wrapper(s). targetCustomPlayer: {targetCustomPlayer != null}, localCustomPlayer: {localCustomPlayer != null}");
                        return false;
                    }

                    // Call targetView.RpcSecure("RPCKicked", RpcTarget.All, true, targetCustomPlayer, localCustomPlayer)
                    Type rpcTargetType = Type.GetType("Photon.Pun.RpcTarget, PhotonUnityNetworking") ?? Type.GetType("RpcTarget, Assembly-CSharp");
                    if (rpcTargetType == null) return false;
                    object rpcTargetAll = Enum.ToObject(rpcTargetType, 0); // 0 corresponds to RpcTarget.All

                    MethodInfo rpcSecureMethod = targetView.GetType().GetMethod("RpcSecure", 
                        BindingFlags.Public | BindingFlags.Instance, 
                        null, 
                        new[] { typeof(string), rpcTargetType, typeof(bool), typeof(object[]) }, 
                        null);

                    if (rpcSecureMethod == null)
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] Kick failed: RpcSecure method not found on PhotonView.");
                        return false;
                    }

                    object[] rpcParams = new object[] {
                        "RPCKicked",
                        rpcTargetAll,
                        true, // encrypt
                        new object[] { targetCustomPlayer, localCustomPlayer }
                    };

                    rpcSecureMethod.Invoke(targetView, rpcParams);
                    return true;
                }
                else
                {
                    matchedName = "multiple";
                    matchesList = string.Join(", ", matchNames.ToArray());
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception in KickPlayer: {ex}");
            }
            return false;
        }

        // ---------------------------------------------------------------
        // Room visibility / max players / private-room-rename (admin commands)
        // ---------------------------------------------------------------

        private static Type GetPhotonNetworkType()
        {
            return Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                   Type.GetType("PhotonNetwork, Assembly-CSharp");
        }

        private static object GetPhotonCurrentRoom()
        {
            try
            {
                Type type = GetPhotonNetworkType();
                PropertyInfo prop = type?.GetProperty("CurrentRoom", BindingFlags.Public | BindingFlags.Static);
                return prop?.GetValue(null);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to read PhotonNetwork.CurrentRoom: {ex.Message}");
                return null;
            }
        }

        // Sets IsVisible only — a private room stays IsOpen so it can still be joined by name.
        private static bool SetRoomVisibility(bool makePrivate, out string roomName, out string error)
        {
            roomName = "";
            error = "";
            object room = GetPhotonCurrentRoom();
            if (room == null) { error = "not currently in a room"; return false; }
            try
            {
                Type roomType = room.GetType();
                PropertyInfo visibleProp = roomType.GetProperty("IsVisible", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                PropertyInfo nameProp = roomType.GetProperty("Name", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                if (visibleProp == null) { error = "IsVisible property not found"; return false; }
                visibleProp.SetValue(room, !makePrivate);
                roomName = nameProp?.GetValue(room) as string ?? "";
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to set room visibility: {ex}");
                return false;
            }
        }

        private static bool TryGetRoomInfo(out bool isVisible, out string roomName, out int maxPlayers, out int playerCount)
        {
            isVisible = true; roomName = ""; maxPlayers = 0; playerCount = 0;
            object room = GetPhotonCurrentRoom();
            if (room == null) return false;
            try
            {
                Type roomType = room.GetType();
                const BindingFlags roomPropFlags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
                isVisible = (bool)(roomType.GetProperty("IsVisible", roomPropFlags)?.GetValue(room) ?? true);
                roomName = roomType.GetProperty("Name", roomPropFlags)?.GetValue(room) as string ?? "";
                maxPlayers = (byte)(roomType.GetProperty("MaxPlayers", roomPropFlags)?.GetValue(room) ?? (byte)0);
                playerCount = (byte)(roomType.GetProperty("PlayerCount", roomPropFlags)?.GetValue(room) ?? (byte)0);
                return true;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to read room info: {ex.Message}");
                return false;
            }
        }

        private static bool SetRoomMaxPlayers(int requested, out int applied, out string error)
        {
            applied = requested;
            error = "";
            object room = GetPhotonCurrentRoom();
            if (room == null) { error = "not currently in a room"; return false; }
            try
            {
                Type roomType = room.GetType();
                PropertyInfo maxProp = roomType.GetProperty("MaxPlayers", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                PropertyInfo countProp = roomType.GetProperty("PlayerCount", BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly);
                if (maxProp == null) { error = "MaxPlayers property not found"; return false; }

                int currentPlayers = countProp != null ? (byte)countProp.GetValue(room) : 0;
                int clamped = Math.Max(requested, Math.Max(currentPlayers, 2));
                clamped = Math.Min(clamped, 255);
                maxProp.SetValue(room, (byte)clamped);
                applied = clamped;
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to set max players: {ex}");
                return false;
            }
        }

        private static bool TryLeaveCurrentRoom()
        {
            try
            {
                Type type = GetPhotonNetworkType();
                MethodInfo leaveMethod = type?.GetMethod("LeaveRoom", BindingFlags.Public | BindingFlags.Static, null, new[] { typeof(bool) }, null);
                if (leaveMethod == null) return false;
                leaveMethod.Invoke(null, new object[] { false });
                return true;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to call PhotonNetwork.LeaveRoom: {ex.Message}");
                return false;
            }
        }

        // Keeps roomOwnedByBot in sync with the actual Photon master client, not just the bot's own
        // create/join history — this is the only way it becomes accurate again after a human
        // manually transfers host to the bot from Liftoff's player list following a by-name join.
        private static void HandleMasterClientSwitched(object newMasterClient)
        {
            try
            {
                if (newMasterClient == null) return;
                FieldInfo localField = newMasterClient.GetType().GetField("IsLocal", BindingFlags.Public | BindingFlags.Instance);
                bool isLocal = localField != null && (bool)localField.GetValue(newMasterClient);
                bool wasOwned = roomOwnedByBot;
                roomOwnedByBot = isLocal;

                if (isLocal && !wasOwned)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Master client switched to this bot — room is now bot-owned.");
                    if (IsClientMode)
                    {
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} You are now the room host — settings/rotation control restored.");
                    }
                    else
                    {
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} This bot is now the room host — settings/rotation control restored.");
                    }
                }
                else if (!isLocal && wasOwned)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] Master client switched away from this bot — room is no longer bot-owned.");
                    if (IsClientMode)
                    {
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Host status was transferred away from you — you no longer own this room — settings/rotation control disabled.");
                    }
                    else
                    {
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Master client switched away from this bot. This bot no longer owns the room — settings/rotation control disabled.");
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Exception in HandleMasterClientSwitched: {ex}");
            }
        }

        private static bool GetPhotonIsConnectedAndReady()
        {
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type != null)
                {
                    PropertyInfo prop = type.GetProperty("IsConnectedAndReady", BindingFlags.Public | BindingFlags.Static);
                    if (prop != null)
                    {
                        return (bool)prop.GetValue(null);
                    }
                }
            }
            catch {}
            return false;
        }

        private static bool GetPhotonBoolProperty(string propertyName)
        {
            try
            {
                Type type = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ?? 
                            Type.GetType("PhotonNetwork, Assembly-CSharp");
                if (type != null)
                {
                    PropertyInfo prop = type.GetProperty(propertyName, BindingFlags.Public | BindingFlags.Static);
                    if (prop != null)
                    {
                        return (bool)prop.GetValue(null);
                    }
                }
            }
            catch {}
            return false;
        }
    }
}
