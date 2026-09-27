"""In-process async worker pool that turns queued jobs into generated wikis.

The pool intentionally uses a plain ``asyncio.Queue``: for the MVP scale
(a couple of concurrent generations per container) it is the simplest thing
that works and stays durable because every state change is written to
``job.json``. Swapping it for Redis/RQ/Celery later only requires replacing
this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.storage import ACTIVE_STATUSES, JobStore, utc_now
from ..core.util import redact_text, redact_url
from ..services import ingestion, packer, publisher
from ..services.wiki_runner import (
    OpenWikiRunError,
    adopt_hidden_wiki,
    decide_mode,
    format_progress,
    prepare_instructions,
    read_run_state,
    run_openwiki,
)

PROGRESS_INTERVAL_SECONDS = 10
RETRYABLE_STATUSES = frozenset({"failed", "cancelled"})


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class JobPool:
    """Async worker pool with N concurrent OpenWiki generations."""

    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        #: Environment overrides for OpenWiki subprocesses (e.g. proxy base URL).
        self.env_overrides: dict[str, str] = {}

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        for index in range(max(1, self.settings.max_concurrent_jobs)):
            task = asyncio.create_task(self._worker(index), name=f"openwiki-worker-{index}")
            self._workers.append(task)

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "queued": self.queue.qsize(),
            "running": len(self._running),
            "workers": len(self._workers),
        }

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def cancel(self, job_id: str) -> str:
        """Cancel a queued or running job.

        Returns ``not_found``, ``not_running``, ``cancelling`` or ``cancelled``.
        """
        job = self.store.get(job_id)
        if job is None:
            return "not_found"
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            self._cancel_requested.add(job_id)
            task.cancel()
            return "cancelling"
        if job.get("status") == "queued":
            await self.store.update(
                job_id, status="cancelled", finished_at=utc_now(), error="cancelled before start"
            )
            return "cancelled"
        return "not_running"

    async def retry(self, job_id: str) -> str:
        """Re-enqueue a failed/cancelled job.

        OpenWiki resumes from ``openwiki/.run.json``, so completed pages are
        kept instead of starting over.
        """
        job = self.store.get(job_id)
        if job is None:
            return "not_found"
        if job.get("status") not in RETRYABLE_STATUSES:
            return "not_retryable"
        await self.store.update(job_id, status="queued", error=None, finished_at=None, progress=None)
        await self.enqueue(job_id)
        return "queued"

    async def recover(self) -> int:
        """Re-queue jobs left active by a previous process.

        OpenWiki persists its page queue in ``openwiki/.run.json``, so a job
        whose workspace survived on disk resumes where it left off.
        """
        requeued = 0
        for job in self.store.list_jobs(limit=10_000):
            if job.get("status") in ACTIVE_STATUSES:
                await self.store.update(job["job_id"], status="queued", error=None)
                await self.enqueue(job["job_id"])
                requeued += 1
        return requeued

    # --- worker loop --------------------------------------------------------
    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self.queue.get()
            self._running.add(job_id)
            task = asyncio.create_task(self._process(job_id), name=f"openwiki-job-{job_id}")
            self._tasks[job_id] = task
            try:
                await task
            except asyncio.CancelledError:
                if job_id in self._cancel_requested:
                    self._cancel_requested.discard(job_id)
                    with contextlib.suppress(Exception):
                        await self.store.update(
                            job_id,
                            status="cancelled",
                            finished_at=utc_now(),
                            error="cancelled by user",
                        )
                else:
                    # Pool shutdown: stop the job, leave it recoverable.
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    raise
            except Exception as exc:  # a worker must never die
                with contextlib.suppress(Exception):
                    await self._fail(job_id, f"unexpected error: {type(exc).__name__}: {exc}")
            finally:
                self._tasks.pop(job_id, None)
                self._running.discard(job_id)
                self.queue.task_done()

    # --- job pipeline -------------------------------------------------------
    async def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.get("status") not in ACTIVE_STATUSES:
            return

        settings = self.settings
        job_dir = self.store.job_dir(job_id)
        repo_dir = self.store.repo_dir(job_id)
        log_path = self.store.logs_path(job_id)
        source = job.get("source") or {}
        token = source.get("token") or None
        secrets: tuple[str, ...] = (token,) if token else ()

        def log(message: str) -> None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"[{_timestamp()}] {redact_text(message, secrets)}\n")

        await self.store.update(job_id, status="fetching", started_at=job.get("started_at") or utc_now(), error=None)

        try:
            await self._fetch(job, job_dir, repo_dir, log)

            if adopt_hidden_wiki(repo_dir, settings.wiki_artifact_dir):
                log(f"adopted {settings.wiki_artifact_dir}/ as openwiki/ for incremental updates")

            requested_mode = str(job.get("mode") or "auto")
            mode = decide_mode(repo_dir, requested_mode)
            if mode == "update" and await ingestion.ensure_full_history(
                repo_dir, url=str(source.get("url") or ""), token=token
            ):
                log("fetched the full history so --update can diff against the last documented commit")
            await self.store.update(job_id, status="generating", mode=mode)
            if requested_mode == "update" and mode == "init":
                log("no existing wiki found in the source; falling back to --init")
            elif mode == "update":
                log("existing wiki detected; running --update (incremental)")

            prepare_instructions(repo_dir, job.get("language"))
            concurrency = job.get("concurrency") or settings.default_page_concurrency
            log(f"starting openwiki --{mode} -p (page concurrency: {concurrency})")

            progress_task = asyncio.create_task(self._track_progress(job_id, repo_dir, log))
            try:
                await run_openwiki(
                    repo_dir,
                    settings=settings,
                    log_path=log_path,
                    mode=mode,
                    page_concurrency=concurrency,
                    secrets=secrets,
                    env_overrides=self.env_overrides,
                )
            finally:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task

            await self.store.update(job_id, status="finalizing")
            pages, size = await asyncio.to_thread(
                packer.pack_wiki,
                repo_dir,
                self.store.wiki_zip_path(job_id),
                root_name=settings.wiki_artifact_dir,
            )
            await self.store.update(job_id, pages=pages, size_bytes=size, progress=f"{pages} pages")
            log(f"wiki ready: {pages} pages, {size} bytes")

            push_options = job.get("push") if isinstance(job.get("push"), dict) else None
            if push_options is not None and source.get("type") == "git":
                if not push_options.get("enabled", True):
                    await self.store.update(
                        job_id,
                        push_result={
                            "status": "skipped",
                            "detail": "push disabled for this job",
                            "at": utc_now(),
                        },
                    )
                    log("push disabled for this job")
                else:
                    push_result = await publisher.publish_wiki(
                        repo_dir,
                        artifact_dir=settings.wiki_artifact_dir,
                        url=str(source.get("url") or ""),
                        token=token,
                        mode=mode,
                        options=push_options,
                        author_name=settings.push_author_name,
                        author_email=settings.push_author_email,
                        log=log,
                    )
                    await self.store.update(job_id, push_result=push_result)
                    log(f"push: {push_result['status']} (branch {push_result.get('branch') or '-'})")

            await self.store.update(job_id, status="done", finished_at=utc_now())
        except publisher.PushError as exc:
            with contextlib.suppress(Exception):
                await self.store.update(
                    job_id,
                    push_result={"status": "failed", "detail": str(exc)[:500], "at": utc_now()},
                )
            await self._fail(job_id, f"push failed: {exc}")
        except (ingestion.IngestionError, OpenWikiRunError, packer.PackError) as exc:
            await self._fail(job_id, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(job_id, f"{type(exc).__name__}: {exc}")

    async def _fetch(
        self,
        job: dict[str, Any],
        job_dir: Path,
        repo_dir: Path,
        log: Callable[[str], None],
    ) -> None:
        source = job.get("source") or {}
        source_type = source.get("type")

        # Resume: a previous attempt already produced the workspace.
        if (repo_dir / ".git").exists():
            log("repository workspace already present; resuming the generation")
            return

        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)

        if source_type == "upload":
            archive = job_dir / str(source.get("archive") or "")
            if not archive.exists():
                raise ingestion.IngestionError(
                    "the uploaded archive is no longer available; submit the job again"
                )
            log(f"extracting {source.get('filename') or archive.name}")
            await asyncio.to_thread(
                ingestion.extract_archive,
                archive,
                repo_dir,
                max_extracted_mb=self.settings.max_extracted_mb,
            )
        else:
            ref = source.get("ref")
            log(f"cloning {redact_url(source.get('url', ''))}" + (f" (ref={ref})" if ref else ""))
            await ingestion.clone_git(
                source.get("url", ""),
                repo_dir,
                ref=ref,
                token=source.get("token") or None,
            )

        initialized = await ingestion.ensure_git_repo(repo_dir)
        if initialized:
            log("source had no .git; initialized a git repository with an initial commit")

    async def _track_progress(
        self,
        job_id: str,
        repo_dir: Path,
        log: Callable[[str], None],
    ) -> None:
        """Refresh ``job.json`` with OpenWiki's own plan progress."""
        last: str | None = None
        announced = False
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
            state = await asyncio.to_thread(read_run_state, repo_dir)
            if state and not announced:
                announced = True
                log(f"openwiki plan: {state['total']} pages planned, phase={state.get('phase')}")
            text = format_progress(state)
            if text is None:
                pages_written = await asyncio.to_thread(packer.count_pages, repo_dir)
                text = format_progress(None, pages_written)
            if text and text != last:
                last = text
                with contextlib.suppress(Exception):
                    await self.store.update(job_id, progress=text)

    async def _fail(self, job_id: str, message: str) -> None:
        with contextlib.suppress(Exception):
            await self.store.update(job_id, status="failed", finished_at=utc_now(), error=message[:500])
        with contextlib.suppress(Exception):
            logs = self.store.logs_path(job_id)
            logs.parent.mkdir(parents=True, exist_ok=True)
            with logs.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"[{_timestamp()}] ! {message}\n")
