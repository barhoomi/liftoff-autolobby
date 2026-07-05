using System.Collections.Generic;
using System.Linq;
using UnityEngine;

namespace LiftoffAutoLobby
{
    public partial class AutoLobbyPlugin
    {
        // Public command — anyone may run it. New in the command-registry refactor.
        // Lists only the commands the sender is permitted to run (filtered by
        // CanExecute, the single permission mechanism), ordered by name, paginated so
        // a page never overflows chat. Formatting uses the theme helpers only.
        private class HelpCommand : IChatCommand
        {
            private const int PageSize = 4;

            public string Name => "/help";
            public string Description => "List available commands. Usage: /help [page]";
            public bool IsAdminOnly => false;

            public bool CanExecute(string userId, bool democracyEnabled, bool roomOwnedByBot) => true;

            public void Execute(string userName, string userId, string argument)
            {
                // Filter to commands this user may run, ordered by name. This mirrors the
                // dispatch gate exactly (same CanExecute), so admin-only / ownership /
                // democracy-gated commands are hidden from users who cannot run them.
                var visibleCommands = CommandRegistry.Commands
                    .Where(cmd => cmd.CanExecute(userId, /* democracyEnabled */ false, roomOwnedByBot))
                    .OrderBy(cmd => cmd.Name, System.StringComparer.OrdinalIgnoreCase)
                    .ToList();

                int totalPages = Mathf.CeilToInt((float)visibleCommands.Count / PageSize);
                if (totalPages < 1) totalPages = 1;

                // Parse requested page; default to 1 on empty, invalid, or out-of-range.
                int page;
                if (!int.TryParse(argument, out page) || page < 1 || page > totalPages)
                    page = 1;

                var pageItems = visibleCommands.Skip((page - 1) * PageSize).Take(PageSize).ToList();

                string header = $"{FormatTag("HELP", activeTheme.infoTagColor)} Commands (Page {page}/{totalPages})";
                if (page < totalPages)
                    header += $" | Type {FormatHighlight($"/help {page + 1}")} for more.";
                SendChatMessage(header);

                foreach (var cmd in pageItems)
                {
                    SendChatMessage($"{FormatHighlight(cmd.Name)} - {cmd.Description}");
                }
            }
        }
    }
}
