"""Call async code (storage) from sync code, wherever that sync code runs.

Inside a request the route runs in an AnyIO worker thread: use the app's event loop.
In the scheduler thread or the CLI there is no loop: run a short-lived one.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import anyio
from anyio import from_thread


def call_async[T](fn: Callable[..., Coroutine[Any, Any, T]], *args: Any) -> T:
    try:
        return from_thread.run(fn, *args)
    except RuntimeError:  # not in an AnyIO worker thread
        return anyio.run(fn, *args)
