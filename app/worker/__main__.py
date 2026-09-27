"""Entrypoint for worker containers: ``python -m app.worker``."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from ..core.config import Settings
from ..core.storage import JobStore
from .loop import WorkerLoop


async def _serve(worker: WorkerLoop) -> None:
    await worker.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await worker.stop()


def main() -> int:
    settings = Settings()
    store = JobStore(settings.data_dir)
    worker = WorkerLoop(settings, store)
    try:
        asyncio.run(_serve(worker))
    except KeyboardInterrupt:  # Windows has no add_signal_handler
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
