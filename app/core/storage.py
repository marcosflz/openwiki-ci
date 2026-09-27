"""Durable wiki metadata storage backed by SQLite.

Each generation is a row in ``<data_dir>/openwiki.db`` while its artifacts live
under ``<data_dir>/jobs/<wiki_id>/`` (workspace, logs, wiki.zip). The database
is the coordination point between the API and the worker containers: claims are
atomic (``BEGIN IMMEDIATE`` plus a conditional update), progress doubles as a
heartbeat and stale claims are requeued by the API, so any number of workers can
share the same volume. WAL mode keeps readers (the API) and writers (workers)
from blocking each other.

The store targets a single host/shared volume. For multi-host scaling, swap this
module for a Postgres-backed implementation with the same methods.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

ACTIVE_STATUSES = frozenset({"queued", "fetching", "generating", "finalizing"})
RUNNING_STATUSES = frozenset({"fetching", "generating", "finalizing"})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

#: Columns callers may change through :meth:`JobStore.update`.
_UPDATABLE = frozenset(
    {
        "status",
        "language",
        "concurrency",
        "mode",
        "resolved_mode",
        "push",
        "claimed_by",
        "claim_id",
        "claimed_at",
        "last_seen_at",
        "cancel_requested",
        "attempts",
        "progress",
        "pages",
        "size_bytes",
        "push_result",
        "error",
        "started_at",
        "finished_at",
    }
)
_JSON_COLUMNS = frozenset({"source", "push", "push_result"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wikis (
    wiki_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    language TEXT,
    concurrency INTEGER,
    mode TEXT NOT NULL DEFAULT 'auto',
    resolved_mode TEXT,
    fingerprint TEXT,
    push TEXT,
    claimed_by TEXT,
    claim_id TEXT,
    claimed_at TEXT,
    last_seen_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    progress TEXT,
    pages INTEGER,
    size_bytes INTEGER,
    push_result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wikis_queue ON wikis (status, created_at);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    last_seen_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_wiki_id() -> str:
    return uuid4().hex


def _seconds_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=max(0, seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobStore:
    """SQLite-backed wiki registry shared by the API and the workers."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "openwiki.db"
        self._lock = threading.RLock()
        # ``isolation_level=None`` turns on autocommit; the claim path opens an
        # explicit BEGIN IMMEDIATE transaction for atomic hand-outs.
        self._connection = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(_SCHEMA)
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(wikis)")}
            if "resolved_mode" not in columns:  # upgrade databases created before these columns
                self._connection.execute("ALTER TABLE wikis ADD COLUMN resolved_mode TEXT")
            if "fingerprint" not in columns:
                self._connection.execute("ALTER TABLE wikis ADD COLUMN fingerprint TEXT")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wikis_fingerprint ON wikis (fingerprint)"
            )
        self.migrate_legacy_files()

    # --- paths --------------------------------------------------------------
    def is_valid_wiki_id(self, wiki_id: str) -> bool:
        return JOB_ID_RE.match(wiki_id or "") is not None

    #: Backwards-compatible alias (older call sites used ``job_id``).
    is_valid_job_id = is_valid_wiki_id

    def job_dir(self, wiki_id: str) -> Path:
        return self.jobs_dir / wiki_id

    def repo_dir(self, wiki_id: str) -> Path:
        return self.job_dir(wiki_id) / "repo"

    def meta_path(self, wiki_id: str) -> Path:
        """Legacy ``job.json`` path (only used for the pre-SQLite migration)."""
        return self.job_dir(wiki_id) / "job.json"

    def logs_path(self, wiki_id: str) -> Path:
        return self.job_dir(wiki_id) / "logs.txt"

    def wiki_zip_path(self, wiki_id: str) -> Path:
        return self.job_dir(wiki_id) / "wiki.zip"

    # --- CRUD ---------------------------------------------------------------
    def create(
        self,
        *,
        source: dict[str, Any],
        language: str | None,
        concurrency: int | None,
        mode: str = "auto",
        push: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        job, _ = self.create_deduplicated(
            fingerprint=fingerprint,
            dedupe=False,
            source=source,
            language=language,
            concurrency=concurrency,
            mode=mode,
            push=push,
        )
        return job

    def create_deduplicated(
        self,
        *,
        fingerprint: str | None,
        dedupe: bool = True,
        source: dict[str, Any],
        language: str | None,
        concurrency: int | None,
        mode: str = "auto",
        push: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a wiki unless an identical one is queued or running.

        Runs in a single ``BEGIN IMMEDIATE`` transaction, so two simultaneous
        submissions cannot both create a job. Returns ``(wiki, created)``;
        ``created=False`` means an active wiki with the same fingerprint was
        reused.
        """
        wiki_id = new_wiki_id()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if dedupe and fingerprint:
                    row = self._connection.execute(
                        "SELECT * FROM wikis WHERE fingerprint=?"
                        " AND status IN ('queued','fetching','generating','finalizing')"
                        " ORDER BY created_at LIMIT 1",
                        (fingerprint,),
                    ).fetchone()
                    if row is not None:
                        self._connection.execute("COMMIT")
                        return self._decode(row), False
                self._connection.execute(
                    "INSERT INTO wikis (wiki_id, status, source, language, concurrency, mode, push,"
                    " fingerprint, created_at) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        wiki_id,
                        json.dumps(source, ensure_ascii=False),
                        language,
                        concurrency,
                        mode,
                        json.dumps(push, ensure_ascii=False) if push is not None else None,
                        fingerprint,
                        utc_now(),
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        job = self.get(wiki_id)
        assert job is not None
        return job, True

    def get(self, wiki_id: str) -> dict[str, Any] | None:
        if not self.is_valid_wiki_id(wiki_id):
            return None
        with self._lock:
            row = self._connection.execute("SELECT * FROM wikis WHERE wiki_id=?", (wiki_id,)).fetchone()
        return self._decode(row)

    def list_wikis(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM wikis ORDER BY created_at DESC, wiki_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def update(self, wiki_id: str, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise ValueError(f"columns not updatable: {sorted(unknown)}")
        if not changes:
            job = self.get(wiki_id)
            if job is None:
                raise KeyError(wiki_id)
            return job
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in changes.items():
            if column in _JSON_COLUMNS:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            elif column == "cancel_requested":
                value = 1 if value else 0
            assignments.append(f"{column}=?")
            values.append(value)
        with self._lock:
            cursor = self._connection.execute(
                f"UPDATE wikis SET {', '.join(assignments)} WHERE wiki_id=?",
                (*values, wiki_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(wiki_id)
        job = self.get(wiki_id)
        assert job is not None
        return job

    def delete(self, wiki_id: str) -> bool:
        if not self.is_valid_wiki_id(wiki_id):
            return False
        with self._lock:
            cursor = self._connection.execute("DELETE FROM wikis WHERE wiki_id=?", (wiki_id,))
        directory = self.job_dir(wiki_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        return cursor.rowcount == 1

    def purge_expired(self, retention_hours: int) -> int:
        """Delete terminal wikis finished more than ``retention_hours`` ago."""
        if retention_hours <= 0:
            return 0
        cutoff = _seconds_ago(retention_hours * 3600)
        with self._lock:
            rows = self._connection.execute(
                "SELECT wiki_id FROM wikis WHERE status IN ('done','failed','cancelled')"
                " AND finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            ).fetchall()
        return sum(1 for row in rows if self.delete(row["wiki_id"]))

    def cleanup_staging(self, max_age_seconds: int = 3600) -> int:
        """Remove abandoned upload staging files."""
        staging = self.data_dir / "staging"
        if not staging.exists():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for path in staging.iterdir():
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        return removed

    # --- worker coordination ------------------------------------------------
    def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        """Atomically hand the oldest queued wiki to ``worker_id``."""
        now = utc_now()
        claim_id = new_wiki_id()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT wiki_id FROM wikis WHERE status='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                cursor = self._connection.execute(
                    "UPDATE wikis SET status='fetching', claimed_by=?, claim_id=?, claimed_at=?,"
                    " last_seen_at=?, cancel_requested=0, attempts=attempts+1,"
                    " started_at=COALESCE(started_at, ?)"
                    " WHERE wiki_id=? AND status='queued'",
                    (worker_id, claim_id, now, now, now, row["wiki_id"]),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        if cursor.rowcount != 1:
            return None
        return self.get(row["wiki_id"])

    def heartbeat(
        self,
        wiki_id: str,
        claim_id: str,
        *,
        status: str | None = None,
        progress: str | None = None,
        pages: int | None = None,
        size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Refresh a claim and return ``{"ok", "cancel", "status"}``.

        ``ok=False`` means the lease was lost (reaped or completed elsewhere),
        so the worker must stop. ``cancel=True`` means the API was asked to
        cancel the generation.
        """
        now = utc_now()
        with self._lock:
            row = self._connection.execute(
                "SELECT status, cancel_requested FROM wikis WHERE wiki_id=? AND claim_id=?",
                (wiki_id, claim_id),
            ).fetchone()
            if row is None or row["status"] not in RUNNING_STATUSES:
                return {"ok": False, "cancel": True, "status": row["status"] if row else None}
            assignments = ["last_seen_at=?"]
            values: list[Any] = [now]
            if status is not None:
                assignments.append("status=?")
                values.append(status)
            if progress is not None:
                assignments.append("progress=?")
                values.append(progress)
            if pages is not None:
                assignments.append("pages=?")
                values.append(pages)
            if size_bytes is not None:
                assignments.append("size_bytes=?")
                values.append(size_bytes)
            self._connection.execute(
                f"UPDATE wikis SET {', '.join(assignments)} WHERE wiki_id=?", (*values, wiki_id)
            )
            return {"ok": True, "cancel": bool(row["cancel_requested"]), "status": status or row["status"]}

    def complete(
        self,
        wiki_id: str,
        claim_id: str,
        *,
        status: str,
        error: str | None = None,
        pages: int | None = None,
        size_bytes: int | None = None,
        push_result: dict[str, Any] | None = None,
    ) -> bool:
        """Store the terminal state of a claim; False when the lease was lost."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal status: {status}")
        now = utc_now()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE wikis SET status=?, error=?, pages=COALESCE(?, pages),"
                " size_bytes=COALESCE(?, size_bytes), push_result=?, finished_at=?,"
                " last_seen_at=?, cancel_requested=0"
                " WHERE wiki_id=? AND claim_id=? AND status IN ('fetching','generating','finalizing')",
                (
                    status,
                    error,
                    pages,
                    size_bytes,
                    json.dumps(push_result, ensure_ascii=False) if push_result is not None else None,
                    now,
                    now,
                    wiki_id,
                    claim_id,
                ),
            )
        return cursor.rowcount == 1

    def recover_stale(self, lease_seconds: int) -> int:
        """Requeue running wikis whose worker stopped sending heartbeats."""
        cutoff = _seconds_ago(lease_seconds)
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE wikis SET status='queued', claimed_by=NULL, claim_id=NULL, claimed_at=NULL,"
                " last_seen_at=NULL, cancel_requested=0"
                " WHERE status IN ('fetching','generating','finalizing')"
                " AND (last_seen_at IS NULL OR last_seen_at < ?)",
                (cutoff,),
            )
        return cursor.rowcount

    def note_worker(self, worker_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO workers (worker_id, last_seen_at) VALUES (?, ?)"
                " ON CONFLICT(worker_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (worker_id, utc_now()),
            )

    def prune_workers(self, max_age_seconds: int = 3600) -> int:
        """Forget workers that have been gone for a while."""
        cutoff = _seconds_ago(max_age_seconds)
        with self._lock:
            cursor = self._connection.execute("DELETE FROM workers WHERE last_seen_at <= ?", (cutoff,))
        return cursor.rowcount

    def stats(self, *, worker_ttl_seconds: int = 120) -> dict[str, int]:
        cutoff = _seconds_ago(worker_ttl_seconds)
        with self._lock:
            row = self._connection.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM wikis WHERE status='queued') AS queued,"
                " (SELECT COUNT(*) FROM wikis WHERE status IN ('fetching','generating','finalizing')) AS running,"
                " (SELECT COUNT(*) FROM workers WHERE last_seen_at >= ?) AS workers",
                (cutoff,),
            ).fetchone()
        return {"queued": row["queued"], "running": row["running"], "workers": row["workers"]}

    # --- API actions --------------------------------------------------------
    def cancel(self, wiki_id: str) -> str:
        """Cancel a wiki: immediate when queued, cooperative when running."""
        job = self.get(wiki_id)
        if job is None:
            return "not_found"
        if job["status"] == "queued":
            self.update(wiki_id, status="cancelled", finished_at=utc_now(), error="cancelled before start")
            return "cancelled"
        if job["status"] in RUNNING_STATUSES:
            self.update(wiki_id, cancel_requested=True)
            return "cancelling"
        return "not_running"

    def retry(self, wiki_id: str) -> str:
        """Requeue a failed/cancelled wiki (OpenWiki resumes where it stopped)."""
        job = self.get(wiki_id)
        if job is None:
            return "not_found"
        if job["status"] not in {"failed", "cancelled"}:
            return "not_retryable"
        self.update(
            wiki_id,
            status="queued",
            error=None,
            progress=None,
            finished_at=None,
            cancel_requested=False,
            claimed_by=None,
            claim_id=None,
            claimed_at=None,
            last_seen_at=None,
        )
        return "queued"

    # --- internals ----------------------------------------------------------
    def _decode(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for column in _JSON_COLUMNS:
            raw = data.get(column)
            if isinstance(raw, str) and raw:
                with contextlib.suppress(json.JSONDecodeError):
                    data[column] = json.loads(raw)
            else:
                data[column] = None
        data["cancel_requested"] = bool(data.get("cancel_requested"))
        return data

    def migrate_legacy_files(self) -> int:
        """Import ``jobs/*/job.json`` records written before the SQLite store."""
        imported = 0
        for path in sorted(self.jobs_dir.glob("*/job.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            wiki_id = str(data.get("wiki_id") or data.get("job_id") or path.parent.name)
            source = data.get("source")
            if not self.is_valid_wiki_id(wiki_id) or not isinstance(source, dict):
                continue
            with self._lock:
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO wikis (wiki_id, status, source, language, concurrency, mode, push,"
                    " claimed_by, claim_id, claimed_at, last_seen_at, cancel_requested, attempts, progress,"
                    " pages, size_bytes, push_result, error, created_at, started_at, finished_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        wiki_id,
                        str(data.get("status") or "queued"),
                        json.dumps(source, ensure_ascii=False),
                        data.get("language"),
                        data.get("concurrency"),
                        str(data.get("mode") or "auto"),
                        json.dumps(data.get("push"), ensure_ascii=False) if data.get("push") is not None else None,
                        data.get("claimed_by"),
                        data.get("claim_id"),
                        data.get("claimed_at"),
                        data.get("last_seen_at"),
                        1 if data.get("cancel_requested") else 0,
                        int(data.get("attempts") or 0),
                        data.get("progress"),
                        data.get("pages"),
                        data.get("size_bytes"),
                        json.dumps(data.get("push_result"), ensure_ascii=False)
                        if data.get("push_result") is not None
                        else None,
                        data.get("error"),
                        str(data.get("created_at") or utc_now()),
                        data.get("started_at"),
                        data.get("finished_at"),
                    ),
                )
            if cursor.rowcount == 1:
                imported += 1
            with contextlib.suppress(OSError):
                path.rename(path.with_name(path.name + ".legacy"))
        return imported
