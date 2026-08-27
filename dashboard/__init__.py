"""Control plane of the Liftoff auto-lobby bot's dashboard.

This repo ships only ``dashboard.control`` — the **service layer**. It owns playlist
resolution and every write to the plain-text plugin protocol files. It is pure Python
with no web dependency, so ``orchestrator/run_headless_lobby.py`` imports and calls it
(decision D5 in ``docs/features/doing/bot-dashboard.md``: the dashboard owns the
control plane, the orchestrator became a caller of it).

The other half — ``dashboard.api``/``write_api``/``__main__``/``static/``, the FastAPI
web app on top of this layer — lives in the private ``liftoff-dashboard`` repo and is
deployed by overlaying its files into this same package directory (see
``dashboard/README.md``). That is why this package keeps the ``dashboard`` name and
layout: the two repos' files interleave into one importable package at deploy time.

Importing ``dashboard`` must never pull in FastAPI: the orchestrator depends on the
control half and runs in environments where the web extras are not installed.
"""
