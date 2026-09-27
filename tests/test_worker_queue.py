"""Unit tests for the SQLite queue shared by the API and the workers."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.storage import JobStore

TOKEN = "ghp_test123"


def _make_store(tmp_path: Path, name: str = "data") -> JobStore:
    return JobStore(tmp_path / name)


def _queue_wiki(store: JobStore, **overrides):
    payload = {
        "source": {"type": "git", "url": "https://github.com/acme/repo.git", "token": TOKEN},
        "language": None,
        "concurrency": None,
    }
    payload.update(overrides)
    return store.create(**payload)


def test_claim_is_atomic_and_exclusive(tmp_path):
    store = _make_store(tmp_path)
    other = _make_store(tmp_path)
    wiki = _queue_wiki(store)

    first = store.claim_next("worker-a")
    assert first is not None
    assert first["wiki_id"] == wiki["wiki_id"]
    assert first["status"] == "fetching"
    assert first["claimed_by"] == "worker-a"
    assert first["attempts"] == 1

    # Another worker (separate connection, same database) sees nothing to do.
    assert other.claim_next("worker-b") is None


def test_heartbeat_cancel_and_complete(tmp_path):
    store = _make_store(tmp_path)
    wiki = _queue_wiki(store)
    claimed = store.claim_next("worker-a")
    claim_id = claimed["claim_id"]

    beat = store.heartbeat(wiki["wiki_id"], claim_id, status="generating", progress="1/3 pages")
    assert beat == {"ok": True, "cancel": False, "status": "generating"}
    store.heartbeat(wiki["wiki_id"], claim_id, size_bytes=1234)
    assert store.get(wiki["wiki_id"])["size_bytes"] == 1234

    assert store.cancel(wiki["wiki_id"]) == "cancelling"
    beat = store.heartbeat(wiki["wiki_id"], claim_id, progress="2/3 pages")
    assert beat["cancel"] is True

    assert store.complete(wiki["wiki_id"], claim_id, status="cancelled", error="cancelled by user")
    assert store.get(wiki["wiki_id"])["status"] == "cancelled"


def test_complete_with_stale_claim_is_ignored(tmp_path):
    store = _make_store(tmp_path)
    wiki = _queue_wiki(store)
    claimed = store.claim_next("worker-a")

    assert store.complete(wiki["wiki_id"], "deadbeef", status="done") is False
    assert store.get(wiki["wiki_id"])["status"] == "fetching"
    assert store.complete(wiki["wiki_id"], claimed["claim_id"], status="done", pages=3) is True
    assert store.get(wiki["wiki_id"])["pages"] == 3


def test_stale_claims_are_requeued(tmp_path):
    store = _make_store(tmp_path)
    wiki = _queue_wiki(store)
    assert store.claim_next("worker-a")["status"] == "fetching"

    # A fresh claim is left alone...
    assert store.recover_stale(180) == 0
    # ...but one without a heartbeat for longer than the lease is requeued.
    store.update(wiki["wiki_id"], last_seen_at="2000-01-01T00:00:00Z")
    assert store.recover_stale(180) == 1
    requeued = store.get(wiki["wiki_id"])
    assert requeued["status"] == "queued"
    assert requeued["claim_id"] is None
    assert requeued["attempts"] == 1


def test_queued_cancel_and_retry(tmp_path):
    store = _make_store(tmp_path)
    wiki = _queue_wiki(store)

    assert store.cancel(wiki["wiki_id"]) == "cancelled"
    assert store.get(wiki["wiki_id"])["status"] == "cancelled"
    assert store.retry(wiki["wiki_id"]) == "queued"
    assert store.get(wiki["wiki_id"])["status"] == "queued"


def test_stats_count_recent_workers(tmp_path):
    store = _make_store(tmp_path)
    _queue_wiki(store)
    store.note_worker("worker-a")

    stats = store.stats()
    assert stats["queued"] == 1
    assert stats["running"] == 0
    assert stats["workers"] == 1


def test_prune_workers(tmp_path):
    store = _make_store(tmp_path)
    store.note_worker("worker-a")

    assert store.prune_workers(0) == 1
    assert store.stats()["workers"] == 0


def test_legacy_job_json_is_migrated(tmp_path):
    data_dir = tmp_path / "data"
    jobs_dir = data_dir / "jobs" / ("a" * 32)
    jobs_dir.mkdir(parents=True)
    legacy = {
        "job_id": "a" * 32,
        "status": "done",
        "source": {"type": "git", "url": "https://github.com/acme/repo.git", "token": TOKEN},
        "language": "es",
        "concurrency": 2,
        "mode": "init",
        "push": None,
        "progress": "4 pages",
        "pages": 4,
        "size_bytes": 1234,
        "error": None,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:01:00Z",
        "finished_at": "2026-01-01T00:05:00Z",
    }
    (jobs_dir / "job.json").write_text(json.dumps(legacy), encoding="utf-8")

    store = JobStore(data_dir)
    migrated = store.get("a" * 32)
    assert migrated is not None
    assert migrated["status"] == "done"
    assert migrated["language"] == "es"
    assert migrated["pages"] == 4
    assert (jobs_dir / "job.json.legacy").exists()

    # Opening the store again does not duplicate or lose the record.
    assert JobStore(data_dir).get("a" * 32)["pages"] == 4
