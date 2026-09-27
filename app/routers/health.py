"""Health and readiness endpoints."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..services.model_status import describe_model
from ..services.wiki_runner import split_command

router = APIRouter(tags=["health"])


@router.get("/health", summary="Service health (fast, never calls the model)")
def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    pool = request.app.state.pool

    binary = ""
    with_argv = split_command(settings.openwiki_bin)
    if with_argv:
        binary = with_argv[0]

    return {
        "status": "ok",
        "version": __version__,
        "openwiki_bin": settings.openwiki_bin,
        "openwiki_found": shutil.which(binary) is not None,
        "data_dir": str(settings.data_dir),
        "jobs": pool.stats,
        "model": describe_model(settings),
    }


@router.get(
    "/health/model",
    summary="Check that the configured model answers (calls the provider)",
    response_description="Probe result; 503 when the model is not configured or does not answer.",
)
async def health_model(
    request: Request,
    force: bool = Query(False, description="Bypass the cached result and probe the provider again."),
) -> JSONResponse:
    result = await request.app.state.model_cache.check(force=force)
    status_code = 200 if result["status"] in {"ok", "unsupported"} else 503
    return JSONResponse(result, status_code=status_code)
