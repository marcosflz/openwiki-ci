"""End-to-end API tests using the fake OpenWiki CLI."""

from __future__ import annotations

import io
import json
import re
import zipfile

from tests.conftest import wait_for_state, wait_for_status


def test_create_wiki_rejects_unsupported_url(client):
    response = client.post("/wikis", json={"source": {"url": "ftp://example.com/repo.git"}})
    assert response.status_code == 422


def test_create_wiki_rejects_empty_url(client):
    response = client.post("/wikis", json={"source": {"url": ""}})
    assert response.status_code == 422


def test_git_wiki_end_to_end(client, make_git_repo):
    repo = make_git_repo()
    response = client.post("/wikis", json={"source": {"url": str(repo)}, "language": "es"})
    assert response.status_code == 202, response.text
    wiki_id = response.json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done", payload
    assert payload["pages"] and payload["pages"] >= 2
    assert payload["source_type"] == "git"
    assert payload["finished_at"]

    download = client.get(f"/wikis/{wiki_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        names = set(zf.namelist())
        assert ".openwiki/index.md" in names
        assert ".openwiki/architecture.md" in names
        # The requested language is written to the generated instructions.
        assert ".openwiki/INSTRUCTIONS.md" in names
        instructions = zf.read(".openwiki/INSTRUCTIONS.md").decode("utf-8")
        assert "`es`" in instructions

    logs = client.get(f"/wikis/{wiki_id}/logs", params={"tail": 50})
    assert logs.status_code == 200
    assert "openwiki" in logs.text
    assert re.search(r"\[\d{2}:\d{2}:\d{2}\]", logs.text)

    listing = client.get("/wikis")
    assert listing.status_code == 200
    assert any(item["wiki_id"] == wiki_id for item in listing.json())


def test_git_wiki_failure_is_reported(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_FAIL", "1")
    repo = make_git_repo("failing-repo")
    response = client.post("/wikis", json={"source": {"url": str(repo)}})
    assert response.status_code == 202

    payload = wait_for_status(client, response.json()["wiki_id"])
    assert payload["status"] == "failed"
    assert "exit" in (payload["error"] or "")
    assert client.get(f"/wikis/{payload['wiki_id']}/download").status_code == 404


def test_wiki_is_not_available_while_running(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "2")
    repo = make_git_repo("slow-repo")
    response = client.post("/wikis", json={"source": {"url": str(repo)}})
    wiki_id = response.json()["wiki_id"]

    early = client.get(f"/wikis/{wiki_id}/download")
    assert early.status_code == 404
    assert "not ready" in early.json()["detail"]

    assert wait_for_status(client, wiki_id)["status"] == "done"


def test_upload_zip_end_to_end(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("src/main.py", "print('hi')\n")
        zf.writestr("README.md", "# hi\n")
    buffer.seek(0)

    response = client.post(
        "/wikis/upload",
        files={"file": ("code.zip", buffer, "application/zip")},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"

    payload = wait_for_status(client, response.json()["wiki_id"])
    assert payload["status"] == "done", payload
    assert payload["source_type"] == "upload"
    assert payload["filename"] == "code.zip"

    download = client.get(f"/wikis/{payload['wiki_id']}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        assert ".openwiki/index.md" in set(zf.namelist())


def test_upload_rejects_unknown_extension(client):
    response = client.post(
        "/wikis/upload",
        files={"file": ("code.rar", io.BytesIO(b"not an archive"), "application/octet-stream")},
    )
    assert response.status_code == 422


def test_delete_wiki(client, make_git_repo):
    repo = make_git_repo("deletable-repo")
    response = client.post("/wikis", json={"source": {"url": str(repo)}})
    wiki_id = response.json()["wiki_id"]
    wait_for_status(client, wiki_id)

    assert client.delete(f"/wikis/{wiki_id}").status_code == 204
    assert client.get(f"/wikis/{wiki_id}").status_code == 404


def test_failed_wiki_can_be_retried(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_FAIL", "1")
    repo = make_git_repo("retry-repo")
    wiki_id = client.post("/wikis", json={"source": {"url": str(repo)}}).json()["wiki_id"]
    assert wait_for_status(client, wiki_id)["status"] == "failed"

    monkeypatch.delenv("FAKE_OPENWIKI_FAIL")
    retry = client.post(f"/wikis/{wiki_id}/retry")
    assert retry.status_code == 202
    assert retry.json()["status"] == "queued"

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done", payload
    assert client.get(f"/wikis/{wiki_id}/download").status_code == 200


def test_cancel_running_wiki_and_retry_resumes(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "10")
    repo = make_git_repo("cancel-running-repo")
    wiki_id = client.post("/wikis", json={"source": {"url": str(repo)}}).json()["wiki_id"]
    wait_for_state(client, wiki_id, "generating")

    cancel = client.post(f"/wikis/{wiki_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelling"

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "cancelled"
    assert "cancelled" in (payload["error"] or "")

    retry = client.post(f"/wikis/{wiki_id}/retry")
    assert retry.status_code == 202
    assert wait_for_status(client, wiki_id, timeout=60)["status"] == "done"


def test_cancel_queued_wiki(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "3")
    first = client.post("/wikis", json={"source": {"url": str(make_git_repo("queued-a"))}}).json()["wiki_id"]
    wait_for_state(client, first, "generating")
    second = client.post("/wikis", json={"source": {"url": str(make_git_repo("queued-b"))}}).json()["wiki_id"]

    cancel = client.post(f"/wikis/{second}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
    assert client.get(f"/wikis/{second}").json()["status"] == "cancelled"

    assert wait_for_status(client, first)["status"] == "done"


def test_cancel_and_retry_validation(client, make_git_repo):
    repo = make_git_repo("cancel-validation-repo")
    wiki_id = client.post("/wikis", json={"source": {"url": str(repo)}}).json()["wiki_id"]
    wait_for_status(client, wiki_id)

    assert client.post(f"/wikis/{wiki_id}/cancel").status_code == 409
    assert client.post(f"/wikis/{wiki_id}/retry").status_code == 409
    assert client.post("/wikis/unknown/cancel").status_code == 404
    assert client.post("/wikis/unknown/retry").status_code == 404


def test_unknown_wiki_returns_404(client):
    assert client.get("/wikis/deadbeef").status_code == 404
    assert client.get("/wikis/..%2Fescape").status_code == 404


def test_token_requires_https_url(client):
    response = client.post(
        "/wikis",
        json={"source": {"url": "git@github.com:org/repo.git", "auth": {"token": "secret"}}},
    )
    assert response.status_code == 422


def _fake_args_from_zip(client, wiki_id: str) -> list[str]:
    download = client.get(f"/wikis/{wiki_id}/download")
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        return json.loads(zf.read(".openwiki/.fake-args.json"))


def test_auto_mode_uses_update_when_the_source_ships_a_wiki(client, make_git_repo):
    repo = make_git_repo("wiki-repo", with_wiki=True)
    wiki_id = client.post("/wikis", json={"source": {"url": str(repo)}}).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done", payload
    assert payload["mode"] == "update"
    assert "--update" in _fake_args_from_zip(client, wiki_id)


def test_auto_mode_uses_init_without_a_wiki(client, make_git_repo):
    repo = make_git_repo("plain-repo")
    wiki_id = client.post("/wikis", json={"source": {"url": str(repo)}}).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done"
    assert payload["mode"] == "init"
    assert "--init" in _fake_args_from_zip(client, wiki_id)


def test_explicit_update_without_wiki_falls_back_to_init(client, make_git_repo):
    repo = make_git_repo("fallback-repo")
    body = {"source": {"url": str(repo)}, "mode": "update"}
    wiki_id = client.post("/wikis", json=body).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done"
    assert payload["mode"] == "init"
    logs = client.get(f"/wikis/{wiki_id}/logs", params={"tail": 50}).text
    assert "falling back to --init" in logs


def test_invalid_mode_is_rejected(client, make_git_repo):
    repo = make_git_repo("bad-mode-repo")
    response = client.post("/wikis", json={"source": {"url": str(repo)}, "mode": "banana"})
    assert response.status_code == 422


def test_hidden_wiki_is_adopted_for_incremental_updates(client, make_git_repo):
    repo = make_git_repo("hidden-wiki-repo", with_wiki=True, wiki_dir=".openwiki")
    wiki_id = client.post("/wikis", json={"source": {"url": str(repo)}}).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done", payload
    assert payload["mode"] == "update"
    assert "--update" in _fake_args_from_zip(client, wiki_id)

    logs = client.get(f"/wikis/{wiki_id}/logs", params={"tail": 50}).text
    assert "adopted .openwiki/ as openwiki/" in logs

    download = client.get(f"/wikis/{wiki_id}/download")
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        assert ".openwiki/index.md" in set(zf.namelist())


def test_completed_wikis_survive_a_restart(settings, make_git_repo):
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = make_git_repo("persistent-repo")
    with TestClient(create_app(settings)) as first:
        response = first.post("/wikis", json={"source": {"url": str(repo)}})
        wiki_id = response.json()["wiki_id"]
        assert wait_for_status(first, wiki_id)["status"] == "done"

    with TestClient(create_app(settings)) as second:
        payload = second.get(f"/wikis/{wiki_id}").json()
        assert payload["status"] == "done"
        assert second.get(f"/wikis/{wiki_id}/download").status_code == 200


def test_active_wikis_are_resumed_after_a_restart(settings, make_git_repo):
    from fastapi.testclient import TestClient

    from app.core.storage import JobStore
    from app.main import create_app

    repo = make_git_repo("resume-repo")
    with TestClient(create_app(settings)) as first:
        response = first.post("/wikis", json={"source": {"url": str(repo)}})
        wiki_id = response.json()["wiki_id"]
        assert wait_for_status(first, wiki_id)["status"] == "done"

    # Simulate a crash mid-generation: running status with a dead claim and no
    # packaged wiki yet.
    store = JobStore(settings.data_dir)
    store.update(
        wiki_id,
        status="generating",
        finished_at=None,
        claimed_by="crashed-worker",
        claim_id="deadbeef",
        last_seen_at=None,
    )
    (settings.data_dir / "jobs" / wiki_id / "wiki.zip").unlink()

    with TestClient(create_app(settings)) as second:
        payload = wait_for_status(second, wiki_id)
        assert payload["status"] == "done", payload
        assert second.get(f"/wikis/{wiki_id}/download").status_code == 200
