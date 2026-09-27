"""Deduplication tests: submitting the same wiki twice reuses the active one."""

from __future__ import annotations

import io
import zipfile

from app.services.fingerprint import canonical_git_url, wiki_fingerprint
from tests.conftest import wait_for_status


def test_canonical_git_url():
    assert canonical_git_url("https://GitHub.com/acme/repo.git") == "https://github.com/acme/repo"
    assert canonical_git_url("https://github.com/acme/repo/") == "https://github.com/acme/repo"
    assert canonical_git_url("https://github.com/acme/repo") == "https://github.com/acme/repo"
    # SSH and local URLs are left untouched.
    assert canonical_git_url("git@github.com:acme/repo.git") == "git@github.com:acme/repo.git"


def test_fingerprint_tracks_language_mode_and_push():
    base = {
        "source": {"type": "git", "url": "https://github.com/acme/repo"},
        "language": "es",
        "mode": "auto",
        "push": None,
    }
    fingerprint = wiki_fingerprint(**base)
    assert fingerprint == wiki_fingerprint(**base)
    assert fingerprint != wiki_fingerprint(**{**base, "language": "en"})
    assert fingerprint != wiki_fingerprint(**{**base, "mode": "init"})
    assert fingerprint != wiki_fingerprint(
        **{**base, "push": {"enabled": True, "branch": "main", "message": "x"}}
    )
    assert fingerprint != wiki_fingerprint(
        **{**base, "source": {"type": "git", "url": "https://github.com/acme/other"}}
    )


def test_duplicate_git_submission_reuses_the_active_wiki(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "2")
    repo = make_git_repo("dedupe-repo")
    body = {"source": {"url": str(repo)}}

    first = client.post("/wikis", json=body)
    second = client.post("/wikis", json=body)

    assert first.status_code == 202
    assert first.json()["deduplicated"] is False
    assert second.status_code == 202
    assert second.json()["deduplicated"] is True
    assert second.json()["wiki_id"] == first.json()["wiki_id"]

    assert wait_for_status(client, first.json()["wiki_id"], timeout=60)["status"] == "done"
    matching = [
        wiki for wiki in client.get("/wikis").json() if "dedupe-repo" in wiki["source_url"]
    ]
    assert len(matching) == 1


def test_force_bypasses_deduplication(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "2")
    repo = make_git_repo("force-repo")
    body = {"source": {"url": str(repo)}}

    first = client.post("/wikis", json=body).json()
    forced = client.post("/wikis?force=true", json=body).json()

    assert forced["deduplicated"] is False
    assert forced["wiki_id"] != first["wiki_id"]
    assert wait_for_status(client, first["wiki_id"], timeout=60)["status"] == "done"
    assert wait_for_status(client, forced["wiki_id"], timeout=60)["status"] == "done"


def test_finished_wikis_do_not_block_new_submissions(client, make_git_repo):
    repo = make_git_repo("finished-repo")
    body = {"source": {"url": str(repo)}}

    first = client.post("/wikis", json=body).json()["wiki_id"]
    assert wait_for_status(client, first)["status"] == "done"

    second = client.post("/wikis", json=body).json()
    assert second["deduplicated"] is False
    assert second["wiki_id"] != first
    assert wait_for_status(client, second["wiki_id"])["status"] == "done"


def _upload(client, payload: bytes, *, force: bool = False, filename: str = "code.zip"):
    url = "/wikis/upload?force=true" if force else "/wikis/upload"
    return client.post(url, files={"file": (filename, io.BytesIO(payload), "application/zip")}).json()


def _zip_bytes(extra: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("README.md", "# hi\n")
        if extra:
            zf.writestr("extra.txt", extra)
    return buffer.getvalue()


def test_duplicate_upload_reuses_the_active_wiki(client, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "2")
    payload = _zip_bytes()

    first = _upload(client, payload)
    second = _upload(client, payload)

    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["wiki_id"] == first["wiki_id"]
    assert wait_for_status(client, first["wiki_id"], timeout=60)["status"] == "done"


def test_different_uploads_are_not_deduplicated(client, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "2")

    first = _upload(client, _zip_bytes())
    second = _upload(client, _zip_bytes("other"))

    assert second["deduplicated"] is False
    assert second["wiki_id"] != first["wiki_id"]
    assert wait_for_status(client, first["wiki_id"], timeout=60)["status"] == "done"
    assert wait_for_status(client, second["wiki_id"], timeout=60)["status"] == "done"
