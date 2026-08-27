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

        // ---------------------------------------------------------------
        // client-ingame-track-change.md (Plan B', code item 1): effect confirmation for an
        // in-flight track apply, and the Photon in-room test the in-flight path needs.
        // ---------------------------------------------------------------

        // True when Photon says we are in a room. The in-flight rotation path cannot use the
        // usual GameObject.Find("GameRoom") test: that object only exists in the
        // MultiplayerMenu scene, not in a flight level (VERDICT code item 2). Thin named
        // wrapper so call sites read as intent rather than as a magic property string.
        private static bool IsPhotonInRoom()
        {
            return GetPhotonBoolProperty("InRoom");
        }

        // Snapshot of the current room's custom properties, rendered as a stable string.
        //
        // Why a string blob instead of reading the Track key directly: F6(b) established that
        // the track rides in the room's custom properties under keys taken from an enum whose
        // *members* are readable but whose key encoding in the Hashtable is written by
        // obfuscated members of CurrentMultiplayerGame — addressing one key by literal would be
        // exactly the guess AGENTS.md rule 1 forbids, and obfuscated names are not stable across
        // game patches. Comparing a before/after snapshot needs no key names at all and matches
        // the semantics of the game's own apply return value (F6a: `num > 0`, i.e. "at least one
        // room property actually changed"). Keys are sorted so ordering noise never reads as a
        // change. Returns null when there is no room / the read fails — callers must treat null
        // as "unknown", never as "changed".
        private static string GetRoomPropertiesSnapshot()
        {
            object room = GetPhotonCurrentRoom();
            if (room == null) return null;
            try
            {
                PropertyInfo propsProp = room.GetType().GetProperty("CustomProperties",
                    BindingFlags.Public | BindingFlags.Instance);
                var table = propsProp?.GetValue(room) as System.Collections.IDictionary;
                if (table == null) return null;
                var entries = new List<string>();
                foreach (System.Collections.DictionaryEntry entry in table)
                {
                    entries.Add($"{entry.Key}={entry.Value}");
                }
                entries.Sort(StringComparer.Ordinal);
                return string.Join("|", entries.ToArray());
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to snapshot room custom properties: {ex.Message}");
                return null;
            }
        }

        // The track the game currently has loaded, by display name, plus its environment.
        //
        // Second, independent effect confirmation (VERDICT confirmation step 2): the room
        // properties say what was published, this says what the local client actually rebuilt.
        // Every name used here survived obfuscation and was confirmed by decompile:
        // `CurrentContentContainer` (a LugusSingletonExisting<> MonoBehaviour) exposes
        // `public Level Level` and `public Track Track`; `Track : TrackQuickInfo : ShareableContent`
        // gives the readable `public string Name { get; set; }` and the readable public field
        // `string environment`. Reached via Resources.FindObjectsOfTypeAll rather than the Lugus
        // generic static, matching the plugin's existing InGameMenuMainPanel access pattern.
        private static bool TryGetCurrentLoadedTrack(out string trackName, out string environment)
        {
            trackName = null;
            environment = null;
            try
            {
                Type containerType = Type.GetType("CurrentContentContainer, Assembly-CSharp");
                if (containerType == null) return false;
                var containers = Resources.FindObjectsOfTypeAll(containerType);
                object container = null;
                foreach (var candidate in containers)
                {
                    if (candidate != null) { container = candidate; break; }
                }
                if (container == null) return false;

                object track = containerType.GetProperty("Track",
                    BindingFlags.Public | BindingFlags.Instance)?.GetValue(container);
                if (track == null) return false;

                Type trackType = track.GetType();
                trackName = trackType.GetProperty("Name",
                    BindingFlags.Public | BindingFlags.Instance)?.GetValue(track) as string;
                environment = trackType.GetField("environment",
                    BindingFlags.Public | BindingFlags.Instance)?.GetValue(track) as string;
                return !string.IsNullOrEmpty(trackName);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to read currently loaded track: {ex.Message}");
                return false;
            }
        }

        // ---------------------------------------------------------------
        // bug-auto-start-joins-running-race.md: "is a race currently running in this room?"
        //
        // WHY THIS SIGNAL. The waiting-room start button is dual-purpose: it reads "Start game"
        // when no race is running and "Join game" while one is. The plugin finds it by NAME
        // (buttonStartGame), so the label never gated the click and every auto-start fired
        // during a race JOINED the bot into it. Decompile of Assembly-CSharp (Liftoff 1.7.4)
        // shows what actually drives that label — the waiting-room panel does
        //     btnStartGame.GetComponentInChildren<Text>().text =
        //         (LugusSingletonExisting<<MpManager>>.use.<IsGameInProgress> ? <"Join game">
        //                                                                     : <"Start game">);
        // and
        //     public bool <IsGameInProgress> => <Ctx>.<CurrentGame>.<Players>
        //         .Any(<PlayerWrapper> p => p.<RoomStatus> == <RoomStatusEnum>.InGamePlaying);
        // i.e. the label switch IS "any player in the room has an in-game room status". Reading
        // that same underlying state — rather than the obfuscated singleton property, whose type
        // name is not stable across game patches — is what this code does.
        //
        // WHERE THE STATE LIVES. The wrapper reads it straight off the Photon player:
        //     public <RoomStatusEnum> <RoomStatus> => <GetProp><<RoomStatusEnum>>(<PropKey>.PlayerRoomStatus);
        //     public T <GetProp><T>(<PropKey> k) { if (<IsUnset>(k)) return (T)<Defaults>[k].<Value>;
        //                                          return (T)<Player>.CustomProperties[<KeyString>(k)]; }
        // and the setter pins the wire format and the key type:
        //     private void <SetProp>(<PropKey> key, object value) {
        //         string k = <PlayerWrapper>.<KeyString>(key);                       // STRING key
        //         if (value is Enum) value = Convert.ChangeType(value,
        //                 value.GetType().GetEnumUnderlyingType());                  // INT value
        //         if (!object.Equals(value, <GetPropObject>(key))) <Props>[k] = value;
        //         else if (<Props>.ContainsKey(k)) <Props>.Remove(k); }              // default => key REMOVED
        // The default for PlayerRoomStatus is <RoomStatusEnum>.None, so an absent key means
        // "not in game" and is safe to skip. Because the value crosses the wire as the enum's
        // UNDERLYING INT, shape-detecting it as an enum (the trick Plugin.Telemetry.cs uses for
        // GameModeState) would not have worked here — the numeric values have to be resolved
        // from the enum type and compared as numbers.
        //
        // WHAT SURVIVED OBFUSCATION (and is therefore safe to key off, AGENTS.md rule 1):
        //   - <RoomStatusEnum>'s members: None/DroneOverview/InWaitingRoom/InGamePlaying/
        //     InGameSpectating/InGameDroneSelection;
        //   - <PropKey>'s members: Ping/PlayerVoiceId/…/PlayerRoomStatus/GameModeState/…;
        //   - the game's own `public static string <KeyString>(<PropKey>)`, which returns
        //     string.Empty when it cannot resolve a key — a reliable failure signal.
        // Type and member NAMES of the classes themselves did not, so none is named here.
        //
        // The in-game set is the game's own per-player predicate (line 531234 of the dump):
        //     public bool <IsInGame> { get { var s = <RoomStatus>;
        //         return s == InGamePlaying || s == InGameSpectating || s == InGameDroneSelection; } }
        // — a superset of the label's InGamePlaying-only test, chosen deliberately: drone
        // selection immediately precedes playing, so suppressing there costs nothing and closes
        // the window where a click would land the bot in a race that is about to run.
        //
        // No state file, no cached "a race is open" flag: this is recomputed from live Photon
        // data on every call, so it self-corrects the instant a race ends (AGENTS.md rules 4-5).
        // ---------------------------------------------------------------
        private static readonly string[] InGameRoomStatusNames =
            { "InGamePlaying", "InGameSpectating", "InGameDroneSelection" };

        // Resolution is attempted exactly once per session: a failure here means a game symbol
        // moved, which retrying every tick cannot fix and would only spam the log with.
        private static bool raceInProgressReflectionAttempted;
        private static string playerRoomStatusPropertyKey;
        private static readonly HashSet<long> inGameRoomStatusValues = new HashSet<long>();

        private static void ResolveRaceInProgressReflection()
        {
            if (raceInProgressReflectionAttempted) return;
            raceInProgressReflectionAttempted = true;
            try
            {
                Assembly asm = Assembly.Load("Assembly-CSharp");
                if (asm == null)
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] Race-in-progress signal could NOT be resolved: Assembly-CSharp not loadable.");
                    return;
                }

                Type[] types;
                try { types = asm.GetTypes(); }
                catch (ReflectionTypeLoadException ex) { types = ex.Types.Where(t => t != null).ToArray(); }

                Type roomStatusEnum = null;
                Type propKeyEnum = null;
                foreach (Type t in types)
                {
                    if (t == null || !t.IsEnum) continue;
                    string[] names;
                    try { names = Enum.GetNames(t); }
                    catch { continue; }
                    if (roomStatusEnum == null && names.Contains("InWaitingRoom") && names.Contains("InGamePlaying")
                        && names.Contains("InGameSpectating") && names.Contains("InGameDroneSelection"))
                    {
                        roomStatusEnum = t;
                    }
                    if (propKeyEnum == null && names.Contains("PlayerRoomStatus") && names.Contains("GameModeState")
                        && names.Contains("DroneConfiguration"))
                    {
                        propKeyEnum = t;
                    }
                    if (roomStatusEnum != null && propKeyEnum != null) break;
                }

                if (roomStatusEnum == null || propKeyEnum == null)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Race-in-progress signal could NOT be resolved: roomStatusEnum={(roomStatusEnum == null ? "MISSING" : roomStatusEnum.FullName)}, playerPropertyKeyEnum={(propKeyEnum == null ? "MISSING" : propKeyEnum.FullName)}. Auto-start stays suppressed this session.");
                    return;
                }

                // The Hashtable key comes from the game's own mapping function, never from a
                // literal: it is looked up in an encrypted-string table, so it is unreadable in
                // IL and not stable across patches. A resolvable MethodInfo is NOT success here
                // (AGENTS.md rule 2) — only a non-empty returned key is, and string.Empty is
                // precisely what the game returns when it cannot resolve the key itself.
                object propKeyValue = Enum.Parse(propKeyEnum, "PlayerRoomStatus");
                string resolvedKey = null;
                foreach (Type t in types)
                {
                    if (t == null) continue;
                    MethodInfo mapper;
                    try
                    {
                        mapper = t.GetMethods(BindingFlags.Public | BindingFlags.Static)
                                  .FirstOrDefault(m => m.ReturnType == typeof(string)
                                                       && m.GetParameters().Length == 1
                                                       && m.GetParameters()[0].ParameterType == propKeyEnum);
                    }
                    catch { continue; }
                    if (mapper == null) continue;
                    try { resolvedKey = mapper.Invoke(null, new object[] { propKeyValue }) as string; }
                    catch { resolvedKey = null; }
                    if (!string.IsNullOrEmpty(resolvedKey)) break;
                }

                if (string.IsNullOrEmpty(resolvedKey))
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] Race-in-progress signal could NOT be resolved: no game function returned a custom-property key for PlayerRoomStatus. Auto-start stays suppressed this session.");
                    return;
                }

                inGameRoomStatusValues.Clear();
                foreach (string name in InGameRoomStatusNames)
                {
                    inGameRoomStatusValues.Add(Convert.ToInt64(Enum.Parse(roomStatusEnum, name)));
                }

                playerRoomStatusPropertyKey = resolvedKey;
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Race-in-progress signal resolved: property key '{resolvedKey}', in-game status values [{string.Join(",", inGameRoomStatusValues.Select(v => v.ToString()).ToArray())}] from {roomStatusEnum.FullName}.");
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Race-in-progress signal could NOT be resolved: {ex.Message}. Auto-start stays suppressed this session.");
            }
        }

        // Returns TRUE when a determination was possible, with raceInProgress carrying it.
        // Returns FALSE for "unknown" — callers must never read that as "no race" (AGENTS.md
        // rule 2). `detail` is always populated and is what the suppression decision event and
        // the click log line report, so a live log always says WHY, not just what.
        private static bool TryDetectRaceInProgress(out bool raceInProgress, out string detail)
        {
            raceInProgress = false;
            detail = "";

            ResolveRaceInProgressReflection();
            if (string.IsNullOrEmpty(playerRoomStatusPropertyKey) || inGameRoomStatusValues.Count == 0)
            {
                detail = "player room-status property is not resolvable in this game build";
                return false;
            }

            try
            {
                Type networkType = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                                   Type.GetType("PhotonNetwork, Assembly-CSharp");
                PropertyInfo playerListProp = networkType?.GetProperty("PlayerList", BindingFlags.Public | BindingFlags.Static);
                Array players = playerListProp?.GetValue(null) as Array;
                if (players == null || players.Length == 0)
                {
                    detail = "Photon PlayerList is empty or unreadable";
                    return false;
                }

                int inGameCount = 0;
                string firstInGameNick = null;
                for (int i = 0; i < players.Length; i++)
                {
                    object playerObj = players.GetValue(i);
                    if (playerObj == null) continue;

                    var props = playerObj.GetType().GetProperty("CustomProperties",
                        BindingFlags.Public | BindingFlags.Instance)?.GetValue(playerObj, null)
                        as System.Collections.IDictionary;
                    if (props == null) continue;

                    // Iterated rather than indexed: the same DictionaryEntry walk
                    // GetRoomPropertiesSnapshot uses, so no assumption is made about how
                    // ExitGames' Hashtable implements key lookup for a string key.
                    object raw = null;
                    foreach (System.Collections.DictionaryEntry entry in props)
                    {
                        if (entry.Key != null && string.Equals(entry.Key.ToString(), playerRoomStatusPropertyKey, StringComparison.Ordinal))
                        {
                            raw = entry.Value;
                            break;
                        }
                    }
                    if (raw == null) continue; // absent/null => the default, <RoomStatusEnum>.None

                    long status;
                    try { status = Convert.ToInt64(raw); }
                    catch { continue; }
                    if (!inGameRoomStatusValues.Contains(status)) continue;

                    inGameCount++;
                    if (firstInGameNick == null)
                    {
                        string nick, userId;
                        ReadPhotonPlayerInfo(playerObj, out nick, out userId);
                        firstInGameNick = string.IsNullOrEmpty(nick) ? "?" : nick;
                    }
                }

                raceInProgress = inGameCount > 0;
                detail = raceInProgress
                    ? $"{inGameCount} of {players.Length} player(s) are in the flight level (first: {firstInGameNick})"
                    : $"none of {players.Length} player(s) are in the flight level";
                return true;
            }
            catch (Exception ex)
            {
                detail = $"read failed: {ex.Message}";
                return false;
            }
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
