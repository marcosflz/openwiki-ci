"""FastAPI application factory.

Run it with the factory flag::

    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .core.config import Settings
from .core.storage import JobStore
from .routers import health, wikis
from .services.model_status import ModelStatusCache
from .services.provider_proxy import ProxyHandle, parse_extra_headers, start_proxy
from .workers.pool import JobPool


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    # Fail fast on malformed extra headers (only when the feature is configured).
    extra_headers = (
        parse_extra_headers(settings.openai_compatible_extra_headers)
        if settings.openai_compatible_extra_headers
        else {}
    )

    store = JobStore(settings.data_dir)
    pool = JobPool(settings, store)
    model_cache = ModelStatusCache(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        proxy: ProxyHandle | None = None
        try:
            if extra_headers and settings.openai_compatible_base_url:
                # OpenWiki cannot send custom headers itself; point it at a
                # loopback proxy that injects them into every request.
                proxy = await start_proxy(
                    settings.openai_compatible_base_url,
                    extra_headers,
                    port=settings.compat_proxy_port,
                )
                pool.env_overrides = {"OPENAI_COMPATIBLE_BASE_URL": proxy.base_url}
            app.state.proxy = proxy
            store.purge_expired(settings.job_retention_hours)
            await pool.start()
            await pool.recover()
            yield
        finally:
            await pool.stop()
            if proxy is not None:
                await proxy.stop()

    app = FastAPI(
        title="OpenWiki Service",
        version=__version__,
        description=(
            "Submit a Git repository (public, or private with a personal access token) "
            "or an uploaded zip/tar archive and download the documentation generated "
            "by the OpenWiki CLI as a zip."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.pool = pool
    app.state.model_cache = model_cache
    app.state.proxy = None

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
