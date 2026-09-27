"""Job endpoints: submit repositories, poll status, download generated wikis."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse

from ..core.storage import TERMINAL_STATUSES
from ..schemas import JobAccepted, JobCreate, JobStatus, JobView, job_to_view
from ..services import ingestion

router = APIRouter(prefix="/jobs", tags=["jobs"])

MAX_LOG_TAIL = 2000


def _accepted(job_id: str) -> JobAccepted:
    base = f"/jobs/{job_id}"
    return JobAccepted(
        job_id=job_id,
        status=JobStatus.queued,
        links={
            "status": base,
            "logs": f"{base}/logs",
            "wiki": f"{base}/wiki.zip",
        },
    )


@router.get("", response_model=list[JobView], summary="List jobs (newest first)")
def list_jobs(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
) -> list[JobView]:
    store = request.app.state.store
    return [job_to_view(job) for job in store.list_jobs(limit=limit)]


@router.post(
    "",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a git repository (public, or private with a token)",
)
async def create_job(payload: JobCreate, request: Request) -> JobAccepted:
    settings = request.app.state.settings
    try:
        url = ingestion.validate_git_url(payload.source.url, allow_local=settings.allow_local_git)
    except ingestion.IngestionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    token = payload.source.auth.token if payload.source.auth else None
    if token and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth.token is only supported for http(s) git URLs",
        )
    if payload.push is not None and payload.push.enabled:
        lowered = url.lower()
        if lowered.startswith(("git@", "ssh://")):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="push requires an http(s) URL; SSH keys are not available in the service",
            )
        if lowered.startswith("git://"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="push is not supported over the git:// protocol",
            )
        if lowered.startswith(("http://", "https://")) and not token:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="push requires source.auth.token (a PAT with write access)",
            )

    source = {"type": "git", "url": url, "ref": payload.source.ref, "token": token}
    job = request.app.state.store.create(
        source=source,
        language=payload.language,
        concurrency=payload.concurrency,
        mode=payload.mode,
        push=payload.push.model_dump() if payload.push is not None else None,
    )
    await request.app.state.pool.enqueue(job["job_id"])
    return _accepted(job["job_id"])


@router.post(
    "/upload",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit an uploaded zip/tar archive",
)
async def upload_source(
    request: Request,
    file: UploadFile = File(..., description="zip/tar archive containing the codebase"),
    language: str | None = Form(None),
    concurrency: int | None = Form(None, ge=1, le=8),
    mode: Literal["auto", "init", "update"] | None = Form(None),
) -> JobAccepted:
    settings = request.app.state.settings
    filename = Path(file.filename or "upload").name
    suffix = ingestion.archive_suffix(filename)
    if suffix is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "unsupported archive type; supported: "
                + ", ".join(ingestion.SUPPORTED_ARCHIVE_SUFFIXES)
            ),
        )

    store = request.app.state.store
    job = store.create(
        source={"type": "upload", "filename": filename, "archive": f"upload{suffix}"},
        language=language,
        concurrency=concurrency,
        mode=mode or "auto",
    )
    dest = store.job_dir(job["job_id"]) / f"upload{suffix}"
    try:
        await ingestion.save_upload(
            _upload_chunks(file),
            dest,
            max_bytes=settings.max_upload_mb * 1024 * 1024,
        )
    except ingestion.IngestionError as exc:
        store.delete(job["job_id"])
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc

    await request.app.state.pool.enqueue(job["job_id"])
    return _accepted(job["job_id"])


async def _upload_chunks(file: UploadFile, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        yield chunk


@router.post("/{job_id}/cancel", summary="Cancel a queued or running job")
async def cancel_job(job_id: str, request: Request) -> dict[str, str]:
    outcome = await request.app.state.pool.cancel(job_id)
    if outcome == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    if outcome == "not_running":
        job = request.app.state.store.get(job_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"job is not running (status={job.get('status') if job else 'unknown'})",
        )
    return {"job_id": job_id, "status": outcome}


@router.post(
    "/{job_id}/retry",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-enqueue a failed/cancelled job (OpenWiki resumes where it stopped)",
)
async def retry_job(job_id: str, request: Request) -> JobAccepted:
    outcome = await request.app.state.pool.retry(job_id)
    if outcome == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    if outcome == "not_retryable":
        job = request.app.state.store.get(job_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "only failed or cancelled jobs can be retried "
                f"(status={job.get('status') if job else 'unknown'})"
            ),
        )
    return _accepted(job_id)


@router.get("/{job_id}", response_model=JobView, summary="Get job status")
def get_job(job_id: str, request: Request) -> JobView:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    return job_to_view(job)


@router.get(
    "/{job_id}/logs",
    response_class=PlainTextResponse,
    summary="Tail the OpenWiki run log",
)
def get_logs(
    job_id: str,
    request: Request,
    tail: int = Query(50, ge=1, le=MAX_LOG_TAIL),
) -> PlainTextResponse:
    store = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    path = store.logs_path(job_id)
    if not path.exists():
        return PlainTextResponse("")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = deque(handle, maxlen=tail)
    return PlainTextResponse("".join(lines))


@router.get(
    "/{job_id}/wiki.zip",
    response_class=FileResponse,
    summary="Download the generated wiki",
)
def download_wiki(job_id: str, request: Request) -> FileResponse:
    store = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    path = store.wiki_zip_path(job_id)
    if not path.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"wiki is not ready (status={job.get('status')})",
        )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"openwiki-{job_id}.zip",
    )


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a finished job")
def delete_job(job_id: str, request: Request) -> None:
    store = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    if job.get("status") not in TERMINAL_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"job is still running (status={job.get('status')})",
        )
    store.delete(job_id)
