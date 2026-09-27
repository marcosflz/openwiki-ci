"""The generation pipeline shared by every worker.

``run_pipeline`` executes one claimed wiki: fetch the source, choose init vs
update, run the OpenWiki CLI, package the zip and optionally push it back. It
reports through the SQLite store (heartbeats and terminal state) and appends
human-readable lines to the wiki log file, so the API only reads what the
worker wrote.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.storage import JobStore, utc_now
from ..core.util import redact_text, redact_url
from . import ingestion, packer, publisher
from .wiki_runner import (
    OpenWikiRunCancelled,
    adopt_hidden_wiki,
    decide_mode,
    prepare_instructions,
    run_openwiki,
)


class PipelineCancelled(Exception):
    """Raised when a generation must stop (user cancel or lost lease)."""


def make_logger(log_path: Path, secrets: tuple[str, ...] = ()) -> Callable[[str], None]:
    """Return a ``log(message)`` callable that timestamps and redacts."""

    def log(message: str) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"[{stamp}] {redact_text(message, secrets)}\n")

    return log


async def run_pipeline(
    *,
    wiki: dict[str, Any],
    settings: Settings,
    store: JobStore,
    log: Callable[[str], None],
    cancel_event: asyncio.Event,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one claimed wiki and return ``{mode, pages, size_bytes, push_result}``."""
    wiki_id = wiki["wiki_id"]
    claim_id = str(wiki.get("claim_id") or "")
    source = wiki.get("source") or {}
    token = source.get("token") or None
    secrets: tuple[str, ...] = (token,) if token else ()
    job_dir = store.job_dir(wiki_id)
    repo_dir = store.repo_dir(wiki_id)

    def check_cancel() -> None:
        if cancel_event.is_set():
            raise PipelineCancelled("cancelled by user")

    def report(**fields: Any) -> None:
        result = store.heartbeat(wiki_id, claim_id, **fields)
        if not result["ok"]:
            raise PipelineCancelled("the worker lease was lost")
        if result["cancel"]:
            cancel_event.set()
            raise PipelineCancelled("cancelled by user")

    check_cancel()
    await _fetch(wiki, settings, job_dir, repo_dir, log)

    check_cancel()
    if adopt_hidden_wiki(repo_dir, settings.wiki_artifact_dir):
        log(f"adopted {settings.wiki_artifact_dir}/ as openwiki/ for incremental updates")

    requested_mode = str(wiki.get("mode") or "auto")
    mode = decide_mode(repo_dir, requested_mode)
    if mode == "update" and await ingestion.ensure_full_history(
        repo_dir, url=str(source.get("url") or ""), token=token
    ):
        log("fetched the full history so --update can diff against the last documented commit")
    report(status="generating")
    store.update(wiki_id, mode=mode)
    if requested_mode == "update" and mode == "init":
        log("no existing wiki found in the source; falling back to --init")
    elif mode == "update":
        log("existing wiki detected; running --update (incremental)")

    prepare_instructions(repo_dir, wiki.get("language"))
    concurrency = wiki.get("concurrency") or settings.default_page_concurrency
    log(f"starting openwiki --{mode} -p (page concurrency: {concurrency})")

    try:
        await run_openwiki(
            repo_dir,
            settings=settings,
            log_path=store.logs_path(wiki_id),
            mode=mode,
            page_concurrency=concurrency,
            secrets=secrets,
            env_overrides=env_overrides,
            cancel_event=cancel_event,
        )
    except OpenWikiRunCancelled as exc:
        raise PipelineCancelled(str(exc)) from exc

    check_cancel()
    report(status="finalizing")
    pages, size = await asyncio.to_thread(
        packer.pack_wiki,
        repo_dir,
        store.wiki_zip_path(wiki_id),
        root_name=settings.wiki_artifact_dir,
    )
    report(pages=pages)
    log(f"wiki ready: {pages} pages, {size} bytes")

    push_result: dict[str, Any] | None = None
    push_options = wiki.get("push") if isinstance(wiki.get("push"), dict) else None
    if push_options is not None and source.get("type") == "git":
        if not push_options.get("enabled", True):
            push_result = {
                "status": "skipped",
                "detail": "push disabled for this wiki",
                "at": utc_now(),
            }
            log("push disabled for this wiki")
        else:
            check_cancel()
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
            log(f"push: {push_result['status']} (branch {push_result.get('branch') or '-'})")

    return {"mode": mode, "pages": pages, "size_bytes": size, "push_result": push_result}


async def _fetch(
    wiki: dict[str, Any],
    settings: Settings,
    job_dir: Path,
    repo_dir: Path,
    log: Callable[[str], None],
) -> None:
    source = wiki.get("source") or {}
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
                "the uploaded archive is no longer available; submit the wiki again"
            )
        log(f"extracting {source.get('filename') or archive.name}")
        await asyncio.to_thread(
            ingestion.extract_archive,
            archive,
            repo_dir,
            max_extracted_mb=settings.max_extracted_mb,
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
