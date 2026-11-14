"""Runs the real app with uvicorn on a free port for Playwright tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from inleverboks.main import create_app
from tests.conftest import make_settings


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp: Path = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(make_settings(tmp, app_base_url=base_url))
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
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)
