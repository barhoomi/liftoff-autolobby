namespace LiftoffAutoLobby
{
    /// <summary>
    /// Contract for a chat command. Each command is a small class implementing this
    /// interface, registered with <see cref="AutoLobbyPlugin.CommandRegistry"/>.
    ///
    /// <see cref="CanExecute"/> is the single permission mechanism. It folds together
    /// the three checks that used to live as parallel ad hoc conditions in the old
    /// HandleChatCommand switch:
    ///   1. IsAdmin        — is the sender an admin?
    ///   2. democracy-mode — a command that becomes public under democracy mode
    ///                       (forward-looking; see democracy-skip.md — not yet wired).
    ///   3. room-ownership — commands that mutate room state require the bot to own
    ///                       the room (the old "this bot does not own the room" refusal).
    /// The same predicate is reused by /help to decide which commands a given user may
    /// even see, so a command is hidden from users who cannot run it.
    /// </summary>
    public interface IChatCommand
    {
        /// <summary>Command token, lowercase, leading slash. e.g. "/help", "/skip".</summary>
        string Name { get; }

        /// <summary>One-line help description. e.g. "Skip to the next track."</summary>
        string Description { get; }

        /// <summary>Base permission flag: true if only admins may run/see this command.</summary>
        bool IsAdminOnly { get; }

        /// <summary>
        /// Whether the given user may execute (and see) this command right now.
        /// Folds admin, democracy-mode, and room-ownership into one decision.
        /// </summary>
        bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot);

        /// <summary>Run the command. Called only after <see cref="CanExecute"/> passed.</summary>
        void Execute(string userName, string userId, string argument);
    }
}
