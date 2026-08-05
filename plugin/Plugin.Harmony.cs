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
    // Harmony patch application and all patch prefixes/finalizers (inactivity watchdog,
    //     content-type validator bypass, callback-dispatch try-catch wrapper,
    //     ChatMessagePatch chat interception -> HandleChatCommand).
    public partial class AutoLobbyPlugin
    {



        private static void ApplyHarmonyPatches()
        {
            try
            {
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Applying Harmony patches...");
                
                Assembly asm = Assembly.Load("Assembly-CSharp");
                Type shareableType = asm.GetType("ShareableContent") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "ShareableContent");
                if (shareableType == null)
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] Could not find ShareableContent type.");
                    return;
                }

                Type targetType = null;
                MethodInfo targetMethod = null;

                foreach (Type t in asm.GetTypes())
                {
                    if (t.BaseType != typeof(object)) continue;
                    
                    var fields = t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    bool hasListField = fields.Any(f => f.FieldType.IsGenericType && 
                                                        f.FieldType.GetGenericTypeDefinition() == typeof(List<>) && 
                                                        f.FieldType.GetGenericArguments()[0] == shareableType);
                    if (!hasListField) continue;

                    bool hasContentTypeField = fields.Any(f => f.FieldType.Name == "ContentType");
                    if (!hasContentTypeField) continue;

                    var methods = t.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    foreach (var m in methods)
                    {
                        if (m.ReturnType == typeof(bool))
                        {
                            var pars = m.GetParameters();
                            if (pars.Length == 2 && pars[0].ParameterType == shareableType && pars[1].ParameterType == typeof(bool))
                            {
                                targetType = t;
                                targetMethod = m;
                                break;
                            }
                        }
                    }
                    if (targetType != null) break;
                }

                if (targetMethod != null)
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found validator method to patch: {targetType.FullName}::{targetMethod.Name}");
                    
                    var harmony = new HarmonyLib.Harmony("com.barhoomi.liftoff.autolobby.patch");
                    var prefixMethod = typeof(AutoLobbyPlugin).GetMethod("ValidationPrefix", BindingFlags.NonPublic | BindingFlags.Static);
                    
                    if (prefixMethod != null)
                    {
                        harmony.Patch(targetMethod, prefix: new HarmonyLib.HarmonyMethod(prefixMethod));
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Harmony patch applied successfully!");
                    }
                    else
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] ValidationPrefix method not found in plugin.");
                    }
                }
                else
                {
                    UnityEngine.Debug.LogError("[AutoLobbyPlugin] Target validator method not found in Assembly-CSharp.");
                }

                // Patch ChatMessagePatch
                try
                {
                    var chatTarget = ChatMessagePatch.TargetMethod();
                    if (chatTarget != null)
                    {
                        var chatPostfix = typeof(ChatMessagePatch).GetMethod("Postfix", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                        if (chatPostfix != null)
                        {
                            var harmony = new HarmonyLib.Harmony("com.barhoomi.liftoff.autolobby.chat");
                            harmony.Patch(chatTarget, postfix: new HarmonyLib.HarmonyMethod(chatPostfix));
                            UnityEngine.Debug.Log("[AutoLobbyPlugin] ChatMessagePatch applied successfully!");
                        }
                        else
                        {
                            UnityEngine.Debug.LogError("[AutoLobbyPlugin] ChatMessagePatch Postfix method not found.");
                        }
                    }
                    else
                    {
                        UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] ChatWindowPanel.GenerateUserMessage target method not found.");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to patch ChatMessagePatch: {ex}");
                }

                // Patch RaceLinesVisualizer.CreateInstance to suppress null instantiation exceptions
                try
                {
                    Type visualizerType = asm.GetType("RaceLinesVisualizer") ?? asm.GetTypes().FirstOrDefault(t => t.Name == "RaceLinesVisualizer");
                    if (visualizerType != null)
                    {
                        MethodInfo createInstanceMethod = visualizerType.GetMethod("CreateInstance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static);
                        if (createInstanceMethod != null)
                        {
                            UnityEngine.Debug.Log("[AutoLobbyPlugin] Found RaceLinesVisualizer.CreateInstance! Patching with finalizer.");
                            var harmony = new HarmonyLib.Harmony("com.barhoomi.liftoff.autolobby.visualizer");
                            var finalizerMethod = typeof(AutoLobbyPlugin).GetMethod("CreateInstanceFinalizer", BindingFlags.NonPublic | BindingFlags.Static);
                            if (finalizerMethod != null)
                            {
                                harmony.Patch(createInstanceMethod, finalizer: new HarmonyLib.HarmonyMethod(finalizerMethod));
                                UnityEngine.Debug.Log("[AutoLobbyPlugin] RaceLinesVisualizer.CreateInstance patch applied successfully!");
                            }
                            else
                            {
                                UnityEngine.Debug.LogError("[AutoLobbyPlugin] CreateInstanceFinalizer method not found.");
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Visualizer patch failed: {ex.Message}");
                }

                // Patch Photon in-room callbacks to prevent visualizer/sync exceptions from crashing the room synchronization
                try
                {
                    Assembly photonRealtimeAsm = Assembly.Load("PhotonRealtime");
                    string[] callbackContainerTypes = photonRealtimeAsm.GetTypes()
                        .Where(t => t.Name.EndsWith("CallbacksContainer") || t.Name.Contains("CallbackContainer"))
                        .Select(t => t.FullName)
                        .ToArray();

                    var harmony = new HarmonyLib.Harmony("com.barhoomi.liftoff.autolobby.photon");
                    var prefixMethod = typeof(AutoLobbyPlugin).GetMethod("PhotonContainerPrefix", BindingFlags.NonPublic | BindingFlags.Static);
                    
                    if (prefixMethod != null)
                    {
                        foreach (string typeName in callbackContainerTypes)
                        {
                            Type containerType = photonRealtimeAsm.GetType(typeName);
                            if (containerType != null)
                            {
                                var methods = containerType.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                                int patchedCount = 0;
                                foreach (var method in methods)
                                {
                                    if (method.Name.StartsWith("On"))
                                    {
                                        harmony.Patch(method, prefix: new HarmonyLib.HarmonyMethod(prefixMethod));
                                        patchedCount++;
                                    }
                                }
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Successfully patched {patchedCount} callbacks on {typeName} with try-catch loop prefix.");
                            }
                            else
                            {
                                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Could not find Photon type: {typeName}");
                            }
                        }
                    }
                    else
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] PhotonContainerPrefix method not found.");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Photon callbacks patching failed: {ex.Message}");
                }

                // Patch the multiplayer waiting-room panel's OnEnable to neutralize Liftoff's
                // real inactivity-kick watchdog. Decompile findings (docs/features/doing/
                // inactivity-kick-prevention.md, "Root Cause Found" section): the waiting-room
                // panel runs a coroutine that counts down from `hostInactivityMinutes * 60`
                // seconds and ONLY resets that countdown when a private Rewired-input singleton
                // reports GetAnyButtonDown() == true on the local player — i.e. a real physical
                // input edge event. It never reads chat sends (SendUserMessage/RPCs) at all, so
                // the plugin's Pro Tip broadcasts (HandleKeepAlive) cannot reset it — the bot has
                // no real input device, so GetAnyButtonDown() is always false for it and the
                // countdown reaches zero, triggering the kick/scene-reload path.
                // The countdown coroutine only reads hostInactivityMinutes once, at the moment it
                // starts (inside OnEnable, synchronously, since Unity runs a coroutine up to its
                // first `yield` in the same call that starts it) — so overwriting the field is
                // only effective if done via a Prefix that runs before the original OnEnable body.
                // No chat-message- or RPC-based reset path exists in the decompiled coroutine, so
                // there's no non-input "authoritative call" analogous to RPCKicked for /kick;
                // this reflection-set of a private serialized field (not input simulation) is the
                // legitimate fix given what's actually in the game code.
                try
                {
                    Type waitingRoomPanelType = asm.GetTypes().FirstOrDefault(t =>
                        t.GetFields(BindingFlags.NonPublic | BindingFlags.Instance)
                         .Any(f => f.Name == "hostInactivityMinutes" && f.FieldType == typeof(int)));

                    if (waitingRoomPanelType != null)
                    {
                        MethodInfo onEnableMethod = waitingRoomPanelType.GetMethod("OnEnable", BindingFlags.NonPublic | BindingFlags.Instance);
                        if (onEnableMethod != null)
                        {
                            var harmony = new HarmonyLib.Harmony("com.barhoomi.liftoff.autolobby.inactivitywatchdog");
                            var prefixMethod = typeof(AutoLobbyPlugin).GetMethod("InactivityWatchdogPrefix", BindingFlags.NonPublic | BindingFlags.Static);
                            if (prefixMethod != null)
                            {
                                harmony.Patch(onEnableMethod, prefix: new HarmonyLib.HarmonyMethod(prefixMethod));
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Inactivity watchdog patch applied to {waitingRoomPanelType.FullName}::OnEnable.");
                            }
                            else
                            {
                                UnityEngine.Debug.LogError("[AutoLobbyPlugin] InactivityWatchdogPrefix method not found.");
                            }
                        }
                        else
                        {
                            UnityEngine.Debug.LogError("[AutoLobbyPlugin] Waiting room panel found but its OnEnable method could not be located.");
                        }
                    }
                    else
                    {
                        UnityEngine.Debug.LogError("[AutoLobbyPlugin] Could not find waiting room panel type (searched for a field named 'hostInactivityMinutes'). Inactivity-kick prevention will NOT work this session.");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Inactivity watchdog patching failed: {ex.Message}");
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Harmony patching failed: {ex}");
            }
        }

        // Effectively-infinite override for the waiting room panel's private
        // `hostInactivityMinutes` field (24 hours — comfortably longer than any bot session
        // between restarts). See the long comment above where this is patched in for why a
        // field override, not a fake input event, is the correct fix here.
        private const int InactivityWatchdogOverrideMinutes = 1440;

        private static bool InactivityWatchdogPrefix(object __instance)
        {
            try
            {
                FieldInfo field = __instance.GetType().GetField("hostInactivityMinutes", BindingFlags.NonPublic | BindingFlags.Instance);
                if (field != null)
                {
                    field.SetValue(__instance, InactivityWatchdogOverrideMinutes);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Overrode hostInactivityMinutes to {InactivityWatchdogOverrideMinutes} before OnEnable starts the AFK countdown coroutine (bot has no real input device to satisfy the real watchdog).");
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] hostInactivityMinutes field not found on waiting room panel instance; AFK watchdog NOT overridden this activation.");
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error overriding hostInactivityMinutes: {ex}");
            }
            return true; // Let the original OnEnable run; it reads the field synchronously
                         // when it starts the AFK coroutine, so it will pick up our override.
        }

        private static bool ValidationPrefix(object __instance, object[] __args, ref bool __result)
        {
            if (__args == null || __args.Length == 0) return true;
            object item = __args[0];
            if (item == null)
            {
                __result = false;
                return false;
            }

            try
            {
                FieldInfo contentField = __instance.GetType().GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                    .FirstOrDefault(f => f.FieldType.Name == "ContentType");

                if (contentField != null)
                {
                    object depotTypeVal = contentField.GetValue(__instance);
                    PropertyInfo typeProp = item.GetType().GetProperty("Type", BindingFlags.Public | BindingFlags.Instance);
                    if (typeProp != null)
                    {
                        object itemTypeVal = typeProp.GetValue(item);
                        if (depotTypeVal.ToString() != itemTypeVal.ToString())
                        {
                            UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Bypassed type mismatch: item '{item}' type '{itemTypeVal}' does not match depot type '{depotTypeVal}'.");
                            __result = false;
                            return false;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in validation prefix: {ex.Message}");
            }

            return true;
        }

        private static Exception CreateInstanceFinalizer(Exception __exception)
        {
            if (__exception != null)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Suppressed exception in RaceLinesVisualizer.CreateInstance: {__exception}");
            }
            return null; // Suppress the exception!
        }

        private static bool PhotonContainerPrefix(object __instance, MethodBase __originalMethod, object[] __args)
        {
            try
            {
                string methodName = __originalMethod.Name;
                if (methodName == "OnLeftRoom" || methodName == "OnDisconnected")
                {
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Photon Callback: {methodName} detected. Immediately resetting lastInRoomTime to trigger lobby recovery.");
                    // lifecycle-event-logging.md: log BEFORE the resets below — LogDisconnectEvent
                    // reads roomCreatedTime, which the next lines clear.
                    LogDisconnectEvent(methodName);
                    lastInRoomTime = DateTime.MinValue;
                    roomCreatedTime = DateTime.MinValue;
                    isLeaving = false;
                }
                else if (methodName == "OnCreateRoomFailed" && __args != null && __args.Length >= 2)
                {
                    // Not gated on pendingPrivateRoomRename: any create attempt (bot startup,
                    // post-disconnect recreate, etc.) can hit a stale/occupied room name, not just
                    // an explicit /private <name> request — always try to recover.
                    HandleCreateRoomFailed((short)__args[0], __args[1] as string);
                }
                else if (methodName == "OnJoinRoomFailed" && joinByNamePanelSubmitted && __args != null && __args.Length >= 2)
                {
                    HandleJoinByNameFailed((short)__args[0], __args[1] as string);
                }
                else if (methodName == "OnCreatedRoom")
                {
                    roomOwnedByBot = true;
                    // democracy-skip.md: a freshly created room starts with no skip votes.
                    skipVotes.Clear();
                    if (pendingPrivateRoomRename)
                    {
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] Private room rename: new room created successfully.");
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room recreated as private. Join name: {FormatVariable($"{pendingPrivateRoomName}")}.");
                    }
                    pendingPrivateRoomRename = false;
                    pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                    pendingJoinByName = false;
                    joinByNamePanelSubmitted = false;
                }
                else if (methodName == "OnJoinedRoom" && joinByNamePanelSubmitted)
                {
                    UnityEngine.Debug.Log("[AutoLobbyPlugin] Joined an existing room by name instead of creating one — bot does not own this room.");
                    roomOwnedByBot = false;
                    // democracy-skip.md: entering a (different) room starts with no skip votes.
                    skipVotes.Clear();
                    if (IsClientMode)
                    {
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} A room named '{FormatVariable($"{pendingPrivateRoomName}")}' already existed — joined it instead of creating a new one. <color={activeTheme.alertTagColor}><i>You are not the room owner and cannot control settings/rotation here.</i></color> Transfer host back to yourself from the player list, or use /private with a different name to create your own room instead.");
                    }
                    else
                    {
                        QueueChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} A room named '{FormatVariable($"{pendingPrivateRoomName}")}' already existed — joined it instead of creating a new one. <color={activeTheme.alertTagColor}><i>This bot is not the room owner and cannot control settings/rotation here.</i></color> Current host: please transfer host to this bot from the player list so it can control settings/rotation, or use /private with a different name to have the bot create its own room instead.");
                    }
                    pendingPrivateRoomRename = false;
                    pendingPrivateRoomRenameStartTime = DateTime.MinValue;
                    pendingJoinByName = false;
                    joinByNamePanelSubmitted = false;
                }
                else if (methodName == "OnMasterClientSwitched" && __args != null && __args.Length >= 1)
                {
                    HandleMasterClientSwitched(__args[0]);
                }
                else if (methodName == "OnPlayerEnteredRoom" && __args != null && __args.Length >= 1)
                {
                    LogPlayerPresenceEvent("player_join", __args[0]);
                }
                else if (methodName == "OnPlayerLeftRoom" && __args != null && __args.Length >= 1)
                {
                    LogPlayerPresenceEvent("player_leave", __args[0]);
                }

                System.Collections.IList list = __instance as System.Collections.IList;
                if (list == null) return true;

                // Copy targets to avoid collection modified exceptions
                object[] targets;
                lock (list)
                {
                    targets = new object[list.Count];
                    list.CopyTo(targets, 0);
                }

                // Find the interface type that defines this callback
                Type interfaceType = null;
                foreach (var iface in __instance.GetType().GetInterfaces())
                {
                    if (iface.Name.EndsWith("Callbacks") || iface.Name.Contains("Callback"))
                    {
                        interfaceType = iface;
                        break;
                    }
                }

                if (interfaceType == null) return true;

                // Resolve the interface method matching name and parameter types
                var paramTypes = __originalMethod.GetParameters().Select(p => p.ParameterType).ToArray();
                MethodInfo interfaceMethod = interfaceType.GetMethod(__originalMethod.Name, paramTypes);
                if (interfaceMethod == null) return true;

                foreach (var callback in targets)
                {
                    if (callback == null) continue;
                    try
                    {
                        interfaceMethod.Invoke(callback, __args);
                    }
                    catch (Exception ex)
                    {
                        // Log the actual underlying exception
                        Exception realEx = ex.InnerException ?? ex;
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Suppressed exception in {interfaceType.Name} listener ({callback.GetType().FullName}): {realEx}");
                    }
                }

                return false; // Skip the original looping method which would abort on exception
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PhotonContainerPrefix: {ex}");
                return true; // Fallback to original method on error
            }
        }

        [HarmonyPatch]
        class ChatMessagePatch
        {
            public static MethodBase TargetMethod()
            {
                Type chatType = FindType("Liftoff.Multiplayer.Chat.ChatWindowPanel");
                if (chatType == null) return null;
                return chatType.GetMethod("GenerateUserMessage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance, null, new Type[] { typeof(string), typeof(string), typeof(string), typeof(UnityEngine.Color) }, null);
            }

            private static bool IsRenderingHistory()
            {
                try
                {
                    string stack = System.Environment.StackTrace;
                    return stack.IndexOf("GenerateChatFromHistory", StringComparison.OrdinalIgnoreCase) >= 0;
                }
                catch
                {
                    return false;
                }
            }

            public static void Postfix(string userId, string userName, string message, UnityEngine.Color ledColor)
            {
                try
                {
                    if (message == null) return;
                    if (IsRenderingHistory()) return;

                    string trimmedMsg = message.Trim();
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Chat received from {userName} (ID: {userId}): {trimmedMsg}");

                    // Structured JSON file event (A3): every rendered (non-replay) chat line.
                    // command is a real JSON bool marking slash-command messages.
                    LogJsonEvent("chat",
                        ("player", userName),
                        ("userId", userId),
                        ("msg", trimmedMsg),
                        ("command", trimmedMsg.StartsWith("/")));

                    if (trimmedMsg.StartsWith("/"))
                    {
                        if (IsDuplicateMessage(userName, trimmedMsg))
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Ignoring duplicate command '{trimmedMsg}' from {userName}");
                            return;
                        }
                        CommandRegistry.Process(userName, userId, trimmedMsg);
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in ChatMessagePatch: {ex}");
                }
            }
        }
    }
}
