"""Worker loop: claim queued wikis from the SQLite queue and run them.

The same loop powers the ``worker`` containers (``python -m app.worker``) and
the optional in-process worker (``RUN_LOCAL_WORKER=true``), so dev and tests
exercise exactly the production pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Callable
from typing import Any

from ..core.config import Settings
from ..core.storage import JobStore, utc_now
from ..services import ingestion, packer, pipeline, publisher
from ..services.pipeline import PipelineCancelled
from ..services.provider_proxy import ProxyHandle, parse_extra_headers, start_proxy
from ..services.wiki_runner import OpenWikiRunError, format_progress, read_run_state


class WorkerLoop:
    """Claims wikis and runs the generation pipeline."""

    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store
        self.worker_id = settings.worker_id.strip() or socket.gethostname()
        self.proxy: ProxyHandle | None = None
        self.env_overrides: dict[str, str] = {}
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        # Some OpenAI-compatible gateways need custom headers OpenWiki cannot
        # send; point the CLI at a loopback proxy handled by this worker.
        extra_headers = (
            parse_extra_headers(self.settings.openai_compatible_extra_headers)
            if self.settings.openai_compatible_extra_headers
            else {}
        )
        if extra_headers and self.settings.openai_compatible_base_url:
            self.proxy = await start_proxy(
                self.settings.openai_compatible_base_url,
                extra_headers,
                port=self.settings.compat_proxy_port,
            )
            self.env_overrides = {"OPENAI_COMPATIBLE_BASE_URL": self.proxy.base_url}
        self.store.note_worker(self.worker_id)
        for index in range(max(1, self.settings.max_concurrent_jobs)):
            self._tasks.append(
                asyncio.create_task(
                    self._runner(index), name=f"openwiki-worker-{self.worker_id}-{index}"
                )
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self.proxy is not None:
            await self.proxy.stop()
            self.proxy = None

    async def _runner(self, index: int) -> None:
        while True:
            self.store.note_worker(self.worker_id)
            wiki = self.store.claim_next(self.worker_id)
            if wiki is None:
                await asyncio.sleep(self.settings.worker_poll_seconds)
                continue
            await self._run(wiki)

    async def _run(self, wiki: dict[str, Any]) -> None:
        wiki_id = wiki["wiki_id"]
        claim_id = str(wiki.get("claim_id") or "")
        source = wiki.get("source") or {}
        token = source.get("token") or None
        log = pipeline.make_logger(self.store.logs_path(wiki_id), (token,) if token else ())
        log(f"claimed by worker {self.worker_id}")
        cancel_event = asyncio.Event()
        tracker = asyncio.create_task(self._track(wiki_id, claim_id, log, cancel_event))
        try:
            result = await pipeline.run_pipeline(
                wiki=wiki,
                settings=self.settings,
                store=self.store,
                log=log,
                cancel_event=cancel_event,
                env_overrides=self.env_overrides,
            )
            self.store.complete(
                wiki_id,
                claim_id,
                status="done",
                pages=result["pages"],
                size_bytes=result["size_bytes"],
                push_result=result.get("push_result"),
            )
        except PipelineCancelled as exc:
            self.store.complete(wiki_id, claim_id, status="cancelled", error=str(exc))
        except publisher.PushError as exc:
            with contextlib.suppress(Exception):
                self.store.complete(
                    wiki_id,
                    claim_id,
                    status="failed",
                    error=f"push failed: {exc}"[:500],
                    push_result={"status": "failed", "detail": str(exc)[:500], "at": utc_now()},
                )
        except (ingestion.IngestionError, OpenWikiRunError, packer.PackError) as exc:
            self.store.complete(wiki_id, claim_id, status="failed", error=str(exc)[:500])
        except asyncio.CancelledError:
            raise  # worker shutting down: the lease reaper requeues the claim
        except Exception as exc:  # a runner must never die
            with contextlib.suppress(Exception):
                self.store.complete(
                    wiki_id,
                    claim_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
        finally:
            tracker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tracker

    async def _track(
        self,
        wiki_id: str,
        claim_id: str,
        log: Callable[[str], None],
        cancel_event: asyncio.Event,
    ) -> None:
        """Heartbeat the claim with OpenWiki's own plan progress."""
        last: str | None = None
        announced = False
        while True:
            await asyncio.sleep(self.settings.worker_progress_seconds)
            repo_dir = self.store.repo_dir(wiki_id)
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
            result = self.store.heartbeat(wiki_id, claim_id, progress=text)
            if not result["ok"] or result["cancel"]:
                cancel_event.set()
                return
