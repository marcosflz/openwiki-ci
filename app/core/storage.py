"""Durable job metadata storage on disk.

Every job owns a directory under ``<data_dir>/jobs/<job_id>`` containing
``job.json`` (metadata), ``repo/`` (source workspace), ``logs.txt`` and, once
finished, ``wiki.zip``. Metadata writes are atomic so a crash cannot corrupt a
job record.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

ACTIVE_STATUSES = frozenset({"queued", "fetching", "generating", "finalizing"})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_job_id() -> str:
    return uuid4().hex


class JobStore:
    """File-backed job registry with atomic updates."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    # --- paths --------------------------------------------------------------
    def is_valid_job_id(self, job_id: str) -> bool:
        return JOB_ID_RE.match(job_id) is not None

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def repo_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "repo"

    def meta_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def logs_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "logs.txt"

    def wiki_zip_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "wiki.zip"

    # --- CRUD ---------------------------------------------------------------
    def create(
        self,
        *,
        source: dict[str, Any],
        language: str | None,
        concurrency: int | None,
        mode: str = "auto",
        push: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = new_job_id()
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "source": source,
            "language": language,
            "concurrency": concurrency,
            "mode": mode,
            "push": push,
            "push_result": None,
            "progress": None,
            "pages": None,
            "size_bytes": None,
            "error": None,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
        }
        self.job_dir(job_id).mkdir(parents=True, exist_ok=False)
        self._write(job)
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not self.is_valid_job_id(job_id):
            return None
        path = self.meta_path(job_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    async def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        async with self._lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(changes)
            self._write(job)
            return job

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        if not self.jobs_dir.exists():
            return jobs
        for child in self.jobs_dir.iterdir():
            if child.is_dir() and self.is_valid_job_id(child.name):
                job = self.get(child.name)
                if job is not None:
                    jobs.append(job)
        jobs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return jobs[:limit]

    def delete(self, job_id: str) -> bool:
        if not self.is_valid_job_id(job_id):
            return False
        directory = self.job_dir(job_id)
        if not directory.exists():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    def purge_expired(self, retention_hours: int) -> int:
        """Delete terminal jobs finished more than ``retention_hours`` ago."""
        if retention_hours <= 0:
            return 0
        removed = 0
        for job in self.list_jobs(limit=10_000):
            if job.get("status") in ACTIVE_STATUSES:
                continue
            finished = job.get("finished_at")
            if not finished:
                continue
            try:
                finished_dt = datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            age_hours = (datetime.now(timezone.utc) - finished_dt).total_seconds() / 3600
            if age_hours > retention_hours and self.delete(job["job_id"]):
                removed += 1
        return removed

    # --- internals ----------------------------------------------------------
    def _write(self, job: dict[str, Any]) -> None:
        path = self.meta_path(job["job_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
