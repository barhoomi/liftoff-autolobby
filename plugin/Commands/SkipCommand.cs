using System;
using System.Collections.Generic;
using System.Reflection;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin-immediate, OR democracy-mode public vote. Migrated verbatim from the old
        // /skip case, then extended per docs/features/doing/democracy-skip.md.
        private class SkipCommand : IChatCommand
        {
            public string Name => "/skip";
            public string Description => "Skip to the next track (admin: instant; democracy mode: majority vote).";
            public bool IsAdminOnly => true;

            // Admins always pass (subject to room ownership, same as every other
            // ownership-mutating command). Non-admins pass only when democracy mode is
            // enabled — CommandRegistry.Process consults CanExecute for non-admins
            // specifically so this command can become public without ever being reachable
            // while democracy mode is off.
            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => (IsAdmin(userId) || democracyEnabled) && roomOwnedByBot;

            public void Execute(string userName, string userId, string argument)
            {
                if (IsAdmin(userId))
                {
                    skipRequested = true;
                    chatWarnedAboutNextRace = false;
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Skipping to next track.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} triggered /skip");
                    return;
                }

                // Democracy vote path. Only reachable when democracyEnabled && roomOwnedByBot
                // (CanExecute already gated it for this non-admin caller).
                if (skipVotes.Contains(userId))
                {
                    SendChatMessage($"{FormatTag("DEMOCRACY", activeTheme.democracyTagColor)} You have already voted to skip this track.");
                    return;
                }

                skipVotes.Add(userId);
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] {userName} ({userId}) cast a democracy skip vote.");

                int activeHumanPlayers = GetActiveHumanPlayerIds().Count;
                lastKnownActiveHumanPlayers = activeHumanPlayers;
                int requiredVotes = Math.Max(1, (activeHumanPlayers / 2) + 1);
                int voteCount = skipVotes.Count;

                if (voteCount >= requiredVotes)
                {
                    TriggerMajoritySkip(voteCount, requiredVotes, "");
                }
                else
                {
                    SendChatMessage($"{FormatTag("DEMOCRACY", activeTheme.democracyTagColor)} Skip vote registered. ({FormatVariable($"{voteCount}/{requiredVotes}")}) votes to skip.");
                }
            }

            // Tracks the room's active-human-player count as of the last time it was
            // observed (an explicit vote cast, or the last CheckDisconnectedVoters tick).
            // Lets CheckDisconnectedVoters detect "the roster just got smaller" even when
            // the departing player was never a voter themselves (see decision note 6 in
            // democracy-skip.md — a non-voter leaving still lowers requiredVotes and must
            // still trigger a re-check, not just a voter leaving).
            private static int lastKnownActiveHumanPlayers = -1;

            private static void TriggerMajoritySkip(int voteCount, int requiredVotes, string afterNote)
            {
                skipRequested = true;
                chatWarnedAboutNextRace = false;
                SendChatMessage($"{FormatTag("DEMOCRACY", activeTheme.democracyTagColor)} Majority vote reached ({FormatVariable($"{voteCount}/{requiredVotes}")}){afterNote}. Skipping to next track.");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Democracy majority vote reached ({voteCount}/{requiredVotes}){afterNote} — skipping.");
                LogJsonEvent("decision", ("kind", "democracy_skip"), ("detail", $"Majority vote reached ({voteCount}/{requiredVotes}){afterNote}"));
            }

            // Called once per tick from HandleGameRoom (see Plugin.cs). Prunes votes cast by
            // players no longer in the room, then — whenever the active-player roster has
            // shrunk at all since it was last observed (not only when the departing player
            // happened to be a voter) — re-evaluates the majority, since a smaller roster
            // lowers requiredVotes and can make an already-cast vote count now win.
            public static void CheckDisconnectedVoters()
            {
                if (!democracyEnabled) return;

                try
                {
                    HashSet<string> currentIds = GetActiveHumanPlayerIds();
                    int currentCount = currentIds.Count;

                    if (skipVotes.Count == 0)
                    {
                        lastKnownActiveHumanPlayers = currentCount; // keep the baseline fresh for the next vote
                        return;
                    }
                    if (skipRequested) return; // already triggered; waiting for rotation to consume it

                    int removed = skipVotes.RemoveWhere(id => !currentIds.Contains(id));
                    bool rosterShrank = lastKnownActiveHumanPlayers >= 0 && currentCount < lastKnownActiveHumanPlayers;
                    lastKnownActiveHumanPlayers = currentCount;

                    if (skipVotes.Count == 0 || (removed == 0 && !rosterShrank)) return;

                    if (removed > 0)
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Democracy: {removed} skip voter(s) left the room; re-evaluating.");

                    int requiredVotes = Math.Max(1, (currentCount / 2) + 1);
                    int voteCount = skipVotes.Count;

                    if (voteCount >= requiredVotes)
                    {
                        TriggerMajoritySkip(voteCount, requiredVotes, " after a player left");
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in SkipCommand.CheckDisconnectedVoters: {ex}");
                }
            }

            // Active (non-bot) player User IDs currently in the room. Same PlayerList
            // reflection pattern as PlayersCommand, EXCEPT IsLocal here is read via GetField,
            // not GetProperty: decompiling Photon.Realtime.Player (plugin/libs/PhotonRealtime.dll)
            // shows `public readonly bool IsLocal;` is a plain field, not a property — using
            // GetProperty("IsLocal") silently returns null and would count the bot itself as
            // an active human player, corrupting the vote majority math. UserId and
            // ActorNumber ARE real properties (auto-properties), so GetProperty is correct
            // for those. See docs/features/doing/democracy-skip.md for the decompile note.
            private static HashSet<string> GetActiveHumanPlayerIds()
            {
                var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                try
                {
                    Type networkType = Type.GetType("Photon.Pun.PhotonNetwork, PhotonUnityNetworking") ??
                                       Type.GetType("PhotonNetwork, Assembly-CSharp");
                    if (networkType == null) return ids;

                    PropertyInfo playerListProp = networkType.GetProperty("PlayerList", BindingFlags.Public | BindingFlags.Static);
                    if (playerListProp == null) return ids;

                    Array playerArray = (Array)playerListProp.GetValue(null);
                    if (playerArray == null) return ids;

                    foreach (object playerObj in playerArray)
                    {
                        if (playerObj == null) continue;
                        Type playerType = playerObj.GetType();

                        FieldInfo localField = playerType.GetField("IsLocal", BindingFlags.Public | BindingFlags.Instance);
                        bool isLocal = localField != null && (bool)localField.GetValue(playerObj);
                        if (isLocal) continue; // exclude the bot itself

                        PropertyInfo userIdProp = playerType.GetProperty("UserId");
                        string uid = userIdProp != null ? userIdProp.GetValue(playerObj, null) as string : null;
                        if (!string.IsNullOrEmpty(uid))
                            ids.Add(uid);
                    }
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in GetActiveHumanPlayerIds: {ex.Message}");
                }
                return ids;
            }
        }
    }
}
