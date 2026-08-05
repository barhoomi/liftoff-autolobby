"""Bot dashboard — monitoring + control web app for the Liftoff auto-lobby bot.

Two halves:

- ``dashboard.control`` — the **service layer**. It owns playlist resolution and every
  write to the plain-text plugin protocol files. It is pure Python with no web
  dependency, so ``orchestrator/run_headless_lobby.py`` imports and calls it (decision
  D5 in ``docs/features/doing/bot-dashboard.md``: the dashboard owns the control plane,
  the orchestrator became a caller of it).
- ``dashboard.api`` — the FastAPI app (read side: SSE over the JSONL event log, current
  state, log browser; write side: playlists + bot controls), which drives the bot only
  through ``dashboard.control``.

Importing ``dashboard`` must never pull in FastAPI: the orchestrator depends on the
control half and runs in environments where the web extras are not installed.
"""
