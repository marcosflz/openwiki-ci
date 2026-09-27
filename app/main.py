"""FastAPI application factory.

Run it with the factory flag::

    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

Generations run in worker processes/containers (``python -m app.worker``); the
API only enqueues, serves state and requeues stale claims. Set
``RUN_LOCAL_WORKER=true`` to run a worker inside the API process (dev/tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .core.config import Settings
from .core.storage import JobStore
from .routers import health, wikis
from .services.model_status import ModelStatusCache
from .services.provider_proxy import parse_extra_headers
from .worker.loop import WorkerLoop

logger = logging.getLogger("openwiki")


async def _reap_loop(store: JobStore, settings: Settings) -> None:
    """Requeue claims whose worker stopped sending heartbeats."""
    while True:
        await asyncio.sleep(settings.worker_reap_seconds)
        with contextlib.suppress(Exception):
            requeued = store.recover_stale(settings.worker_lease_seconds)
            if requeued:
                logger.warning("requeued %s stale wiki claim(s)", requeued)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    # Fail fast on malformed extra headers (only when the feature is configured).
    if settings.openai_compatible_extra_headers:
        parse_extra_headers(settings.openai_compatible_extra_headers)

    store = JobStore(settings.data_dir)
    model_cache = ModelStatusCache(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker: WorkerLoop | None = None
        store.purge_expired(settings.job_retention_hours)
        recovered = store.recover_stale(settings.worker_lease_seconds)
        if recovered:
            logger.warning("requeued %s stale wiki claim(s) at startup", recovered)
        reaper = asyncio.create_task(_reap_loop(store, settings), name="openwiki-reaper")
        try:
            if settings.run_local_worker:
                worker = WorkerLoop(settings, store)
                await worker.start()
            app.state.worker = worker
            app.state.proxy = worker.proxy if worker else None
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            if worker is not None:
                await worker.stop()

    app = FastAPI(
        title="OpenWiki Service",
        version=__version__,
        description=(
            "Submit a Git repository (public, or private with a personal access token) "
            "or an uploaded zip/tar archive and download the documentation generated "
            "by the OpenWiki CLI as a zip. Generations run in scalable worker "
            "containers that share a SQLite queue on the data volume."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.worker = None
    app.state.proxy = None
    app.state.model_cache = model_cache

    app.include_router(health.router)
    app.include_router(wikis.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": "openwiki-service",
            "docs": "/docs",
            "health": "/health",
            "wikis": "/wikis",
        }

    return app
