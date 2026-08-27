"""``python -m dashboard`` — run the dashboard with uvicorn.

Deliberately refuses to start without a token (see ``control/settings.py``): the app can
rewrite the bot's rotation and schedule a game shutdown, so "no token configured" is a
configuration error, not a permissive default.
"""

import argparse
import os
import sys

from .control.settings import load_settings

TOKEN_HELP = """\
No dashboard token configured. Set one of:

  FPV_DASHBOARD_TOKEN=<secret>            (environment; preferred for Docker/systemd)
  config/lobby_config.json ->  "dashboard": {"token": "<secret>"}

Generate one with:  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
"""


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m dashboard",
                                     description="Liftoff bot dashboard (monitoring + control)")
    parser.add_argument("--host", default=None, help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8770).")
    parser.add_argument("--reload", action="store_true", help="uvicorn auto-reload (development).")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    if not settings.token:
        print(TOKEN_HELP, file=sys.stderr)
        return 2

    import uvicorn

    from .api import create_app

    print(f"[Dashboard] serving on http://{settings.host}:{settings.port}")
    print(f"[Dashboard] container restart button: "
          f"{'enabled -> ' + ' '.join(settings.restart_command) if settings.restart_available else 'disabled'}")
    if args.reload:
        # uvicorn can only reload when given an import string, not an app instance (it
        # silently warns and serves without reloading otherwise). create_app() already
        # defaults to load_settings(), so hand uvicorn the factory and export any CLI
        # host/port override through the same env vars load_settings() reads -- the
        # reloader re-imports in a fresh subprocess where argparse's values don't exist.
        #
        # timeout_graceful_shutdown: without it uvicorn waits FOREVER for open
        # connections on reload -- one dashboard browser tab (long-lived SSE stream)
        # left the live worker stuck in "Waiting for connections to close" on every
        # deploy (hit 2026-08-27). 5s lets normal requests finish and then cuts
        # stragglers; the SSE client reconnects on its own.
        os.environ["FPV_DASHBOARD_HOST"] = settings.host
        os.environ["FPV_DASHBOARD_PORT"] = str(settings.port)
        uvicorn.run("dashboard.api:create_app", factory=True,
                    host=settings.host, port=settings.port,
                    reload=True, log_level="info",
                    timeout_graceful_shutdown=5)
    else:
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                    log_level="info",
                    timeout_graceful_shutdown=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
