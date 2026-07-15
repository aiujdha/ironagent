from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import TypeVar

T = TypeVar("T")

_loop_lock = threading.Lock()
_loop_thread: threading.Thread | None = None
_background_loop: asyncio.AbstractEventLoop | None = None


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _get_background_loop() -> asyncio.AbstractEventLoop:
    global _loop_thread, _background_loop
    with _loop_lock:
        if _background_loop is None:
            _background_loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_run_loop,
                args=(_background_loop,),
                name="ironagent-async-bridge",
                daemon=True,
            )
            _loop_thread.start()
        return _background_loop


def run_sync(coroutine: Coroutine[object, object, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _get_background_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result()
    raise RuntimeError("run_sync cannot be used while an event loop is already running")
