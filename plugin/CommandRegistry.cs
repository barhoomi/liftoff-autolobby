using System;
using System.Collections.Generic;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        /// <summary>
        /// Registry + dispatcher for chat commands. Replaces the old hardcoded
        /// HandleChatCommand switch. Nested in <see cref="AutoLobbyPlugin"/> so it can
        /// reach the plugin's private statics (IsAdmin, roomOwnedByBot, SendChatMessage,
        /// FormatTag, LogEvent, lastActivityTime, activeTheme).
        /// </summary>
        public static class CommandRegistry
        {
            private static readonly Dictionary<string, IChatCommand> commands =
                new Dictionary<string, IChatCommand>(StringComparer.OrdinalIgnoreCase);

            /// <summary>All registered commands (used by /help for filtering).</summary>
            public static IEnumerable<IChatCommand> Commands => commands.Values;

            public static void RegisterCommand(IChatCommand cmd)
            {
                if (cmd == null || string.IsNullOrEmpty(cmd.Name)) return;
                commands[cmd.Name] = cmd;
            }

            /// <summary>
            /// Instantiate and register every built-in command. Called once from Awake.
            /// </summary>
            public static void Initialize()
            {
                commands.Clear();
                RegisterCommand(new HelpCommand());
                RegisterCommand(new VersionCommand());
                RegisterCommand(new InfoCommand());
                RegisterCommand(new HistoryCommand());
                RegisterCommand(new SkipCommand());
                RegisterCommand(new PrevCommand());
                RegisterCommand(new TracksCommand());
                RegisterCommand(new TrackCommand());
                RegisterCommand(new PromoteCommand());
                RegisterCommand(new PlayersCommand());
                RegisterCommand(new IntervalCommand());
                RegisterCommand(new ExtendCommand());
                RegisterCommand(new ShuffleCommand());
                RegisterCommand(new PlaylistCommand());
                RegisterCommand(new ModeCommand());
                RegisterCommand(new KickCommand());
                RegisterCommand(new MaintenanceCommand());
                RegisterCommand(new PrivateCommand());
                RegisterCommand(new PublicCommand());
                RegisterCommand(new MaxPlayersCommand());
                RegisterCommand(new ReloadThemeCommand());
                RegisterCommand(new DemocracyCommand());
                RegisterCommand(new StartCommand());
                RegisterCommand(new StopCommand());
                RegisterCommand(new PauseCommand());
                RegisterCommand(new ResumeCommand());
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] CommandRegistry initialized with {commands.Count} command(s).");
            }

            /// <summary>
            /// Parse and dispatch a chat command. Preserves the exact gating behavior of
            /// the old HandleChatCommand:
            ///   - Public commands (IsAdminOnly == false, e.g. /info, /help) run for anyone.
            ///   - Non-admins issuing any other command are silently ignored (anti-probing).
            ///   - Admins bump lastActivityTime, then unknown admin commands are logged and
            ///     dropped (the old 'default' case).
            ///   - The single CanExecute gate folds the old admin + room-ownership checks:
            ///     for an admin, a CanExecute failure on an ownership command produces the
            ///     "this bot does not own the room" refusal, exactly as before.
            /// </summary>
            public static void Process(string userName, string userId, string cmdText)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Processing command from {userName} ({userId}): {cmdText}");
                try
                {
                    string[] parts = cmdText.Split(new char[] { ' ' }, 2, StringSplitOptions.RemoveEmptyEntries);
                    string cmd = parts[0].ToLower();
                    string arg = parts.Length > 1 ? parts[1].Trim() : "";
                    LogEvent("chat_command", ("cmd", cmd), ("arg", arg), ("user_name", userName), ("user_id", userId));

                    IChatCommand command;
                    commands.TryGetValue(cmd, out command);

                    // Public commands (/info, /help) run before any admin gate, exactly as
                    // /info did in the old switch.
                    if (command != null && !command.IsAdminOnly)
                    {
                        command.Execute(userName, userId, arg);
                        return;
                    }

                    // Everything else is admin-only by default. Non-admins are silently
                    // ignored to prevent probing, UNLESS the command's own CanExecute opts
                    // them in under the current democracy state (e.g. /skip becomes a public
                    // vote when democracy mode is on — see democracy-skip.md). This is the
                    // only place a non-admin's CanExecute is consulted; every other
                    // admin-only command's CanExecute still gates on IsAdmin(userId)
                    // internally, so this is a no-op for them (identical to the old
                    // silently-ignored behavior).
                    if (!IsAdmin(userId))
                    {
                        if (command != null && command.CanExecute(userId, democracyEnabled, roomOwnedByBot))
                        {
                            command.Execute(userName, userId, arg);
                        }
                        else
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Ignoring command '{cmd}' from non-admin {userName} ({userId})");
                        }
                        return;
                    }

                    lastActivityTime = DateTime.UtcNow;

                    // Unknown admin command — old 'default' case: log and drop.
                    if (command == null)
                    {
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Unknown admin command '{cmd}' from {userName}");
                        return;
                    }

                    // Single permission gate. The user is already a confirmed admin here, so a
                    // CanExecute failure means the command's room-ownership requirement is unmet;
                    // reproduce the old pre-switch "this bot does not own the room" refusal.
                    if (!command.CanExecute(userId, democracyEnabled, roomOwnedByBot))
                    {
                        SendChatMessage($"{FormatTag("ADMIN", activeTheme.adminTagColor)} <color={activeTheme.alertTagColor}>'{cmd}' cannot be executed — this bot does not own the room.</color> Transfer host to the bot from the player list, or use /private <name> to have it create/join a different room.");
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] Refusing '{cmd}' from {userName} — bot does not own the room.");
                        return;
                    }

                    command.Execute(userName, userId, arg);
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in CommandRegistry.Process: {ex}");
                }
            }
        }
    }
}
