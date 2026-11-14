"""Runs the real app with uvicorn on a free port for Playwright tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from inleverboks.db import migrate
from inleverboks.main import create_app
from tests.conftest import make_settings


@dataclass
class LiveServer:
    url: str
    app: FastAPI

    def __str__(self) -> str:
        return self.url

    def __add__(self, path: str) -> str:
        return self.url + path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveServer]:
    tmp: Path = tmp_path_factory.mktemp("e2e")
    database_url = f"sqlite:///{tmp / 'e2e.db'}"
    migrate.upgrade(database_url)
    port = _free_port()
    # 127.0.0.1 counts as a secure context, so Web Crypto (SHA-256) is available.
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(make_settings(tmp, app_base_url=base_url, database_url=database_url))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            msg = "uvicorn did not start"
            raise RuntimeError(msg)
        time.sleep(0.05)
    yield LiveServer(url=base_url, app=app)
    server.should_exit = True
    thread.join(timeout=5)
