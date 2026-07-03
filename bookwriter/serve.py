"""Launcher: ``python -m bookwriter.serve``.

Runs uvicorn on 127.0.0.1:8000 (override via BOOKWRITER_HOST / BOOKWRITER_PORT)
serving ``bookwriter.server.create_app()``. Keep heavy imports inside main() so
this module stays cheap to import.

The app has **no authentication** and keeps all live state (the SSE broker, the
one-job-per-book lock, the settings store) in process memory, so:
  * it binds 127.0.0.1 by default and refuses a non-local bind unless you set
    BOOKWRITER_ALLOW_REMOTE=1 (put an auth proxy + TLS in front first); and
  * it must run as a SINGLE process — do not start it under multiple uvicorn/
    gunicorn workers, which would split the in-memory state across processes.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def remote_bind_error(host: str, allow_remote: bool) -> Optional[str]:
    """Return a refusal message if binding *host* is unsafe, else ``None``.

    The app has no authentication, so a non-local bind is refused unless the
    operator explicitly opts in (``BOOKWRITER_ALLOW_REMOTE=1``).
    """
    if host in _LOCAL_HOSTS or allow_remote:
        return None
    return (
        f"Refusing to bind {host!r}: BookwriterPro has NO authentication.\n"
        f"Anyone who can reach it could drive paid generation, read/change "
        f"settings, and delete books.\n"
        f"To expose it anyway, set BOOKWRITER_ALLOW_REMOTE=1 AND put it behind "
        f"a reverse proxy that adds authentication and TLS."
    )


def _configure_logging() -> None:
    level = os.environ.get("BOOKWRITER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    # Load ./.env (or BOOKWRITER_ENV_FILE) BEFORE reading any configuration —
    # log level, host/port, keys. Variables already exported in the shell win.
    from .cli import load_env_file
    load_env_file()

    _configure_logging()
    log = logging.getLogger("bookwriter.serve")

    try:
        import uvicorn
    except ImportError:
        sys.exit(
            "Server extras not installed. Run:\n"
            "  pip install -r requirements-server.txt   (or: pip install -e \".[server]\")"
        )

    from .server import create_app

    host = os.environ.get("BOOKWRITER_HOST", "127.0.0.1")
    port = int(os.environ.get("BOOKWRITER_PORT", "8000"))

    # No-auth guard: refuse a non-local bind unless explicitly opted in.
    refusal = remote_bind_error(host, os.environ.get("BOOKWRITER_ALLOW_REMOTE") == "1")
    if refusal:
        sys.exit(refusal)
    if host not in _LOCAL_HOSTS:
        log.warning("Binding %s with NO authentication — ensure an auth proxy + TLS "
                    "are in front of this app.", host)

    # Multi-worker would split the in-memory broker/locks across processes.
    if (os.environ.get("WEB_CONCURRENCY") or "1") != "1":
        log.warning("WEB_CONCURRENCY=%s ignored — BookwriterPro must run as a single "
                    "process (in-memory SSE/state).", os.environ.get("WEB_CONCURRENCY"))

    app = create_app()
    log.info("BookwriterPro server: http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
