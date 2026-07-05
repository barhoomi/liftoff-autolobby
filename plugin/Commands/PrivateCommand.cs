using System;
using System.IO;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Admin. Migrated verbatim from the old /private case.
        //
        // Room-ownership is CONDITIONAL for this command: in the old switch it was in
        // requiresOwnership only when the argument was empty (`/private` with no name
        // toggles the current room's visibility, which needs ownership; `/private <name>`
        // leaves and creates/joins a different room and works regardless of ownership —
        // it is the recovery path out of a non-owned room).
        //
        // CanExecute's signature has no access to the argument, so it cannot express that
        // arg-dependent rule. To preserve behavior EXACTLY, CanExecute gates on admin only
        // (so /private is never blocked at the dispatch gate and stays visible to admins in
        // /help), and the empty-arg ownership refusal is reproduced verbatim here — the
        // same message and ordering (ownership check first, before the switch body) as the
        // old code.
        private class PrivateCommand : IChatCommand
        {
            public string Name => "/private";
            public string Description => "Make the room private, or recreate it with a join name. Usage: /private [name]";
            public bool IsAdminOnly => true;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot)
                => IsAdmin(userId);

            public void Execute(string userName, string userId, string argument)
            {
                // Verbatim of the old pre-switch requiresOwnership refusal, which applied to
                // /private only when the argument was empty.
                if (string.IsNullOrWhiteSpace(argument) && !roomOwnedByBot)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} <color={activeTheme.alertTagColor}>'/private' cannot be executed — this bot does not own the room.</color> Transfer host to the bot from the player list, or use /private <name> to have it create/join a different room.");
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Refusing '/private' from {userName} — bot does not own the room.");
                    return;
                }

                if (pendingPrivateRoomRename)
                {
                    SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} A room rename is already in progress. Please wait for it to finish.");
                }
                else if (string.IsNullOrWhiteSpace(argument))
                {
                    // No name given: just toggle visibility on the current room, name unchanged.
                    string curName;
                    string setErr;
                    if (SetRoomVisibility(true, out curName, out setErr))
                    {
                        try { File.WriteAllText(Path.Combine(pluginPath, "room_private.txt"), "true"); } catch { }
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Room is now private. Join name: {FormatVariable($"{curName}")}.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} set room to private.");
                    }
                    else
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} Could not change visibility: {setErr}. Usage: /private [name] (name recreates the room with that join name).");
                    }
                }
                else
                {
                    BeginPrivateRoomRename(argument.Trim(), userName);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Admin {userName} requested private room rename to '{argument.Trim()}'.");
                }
            }
        }
    }
}
