"""Container entrypoint: optionally migrate, then exec uvicorn.

Written in Python instead of shell so it works on a read-only root filesystem and
quotes nothing by hand.
"""

from __future__ import annotations

import os
import sys

from inleverboks.config import get_settings
from inleverboks.db import migrate as db_migrate

PORT = os.environ.get("PORT", "8080")


def uvicorn_argv(forwarded_allow_ips: str) -> list[str]:
    argv = [
        "uvicorn",
        "inleverboks.asgi:app",
        "--host",
        "0.0.0.0",  # noqa: S104 - container listens on all interfaces
        "--port",
        PORT,
        "--no-server-header",
        "--timeout-graceful-shutdown",
        "30",
    ]
    if forwarded_allow_ips:
        argv += ["--proxy-headers", "--forwarded-allow-ips", forwarded_allow_ips]
    else:
        argv += ["--no-proxy-headers"]
    return argv


def main() -> None:
    settings = get_settings()
    if settings.run_migrations_on_start:
        db_migrate.upgrade(settings.database_url)
    argv = uvicorn_argv(settings.forwarded_allow_ips())
    sys.stdout.flush()
    os.execvp(argv[0], argv)  # noqa: S606 - fixed argv, no shell


if __name__ == "__main__":
    main()
