"""End-to-end API tests using the fake OpenWiki CLI."""

from __future__ import annotations

import io
import json
import re
import zipfile

from tests.conftest import wait_for_state, wait_for_status


def test_create_job_rejects_unsupported_url(client):
    response = client.post("/jobs", json={"source": {"url": "ftp://example.com/repo.git"}})
    assert response.status_code == 422


def test_create_job_rejects_empty_url(client):
    response = client.post("/jobs", json={"source": {"url": ""}})
    assert response.status_code == 422


def test_git_job_end_to_end(client, make_git_repo):
    repo = make_git_repo()
    response = client.post("/jobs", json={"source": {"url": str(repo)}, "language": "es"})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "done", payload
    assert payload["pages"] and payload["pages"] >= 2
    assert payload["source_type"] == "git"
    assert payload["finished_at"]

    download = client.get(f"/jobs/{job_id}/wiki.zip")
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

    logs = client.get(f"/jobs/{job_id}/logs", params={"tail": 50})
    assert logs.status_code == 200
    assert "openwiki" in logs.text
    assert re.search(r"\[\d{2}:\d{2}:\d{2}\]", logs.text)

    listing = client.get("/jobs")
    assert listing.status_code == 200
    assert any(item["job_id"] == job_id for item in listing.json())


def test_git_job_failure_is_reported(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_FAIL", "1")
    repo = make_git_repo("failing-repo")
    response = client.post("/jobs", json={"source": {"url": str(repo)}})
    assert response.status_code == 202

    payload = wait_for_status(client, response.json()["job_id"])
    assert payload["status"] == "failed"
    assert "exit" in (payload["error"] or "")
    assert client.get(f"/jobs/{payload['job_id']}/wiki.zip").status_code == 404


def test_wiki_is_not_available_while_running(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "2")
    repo = make_git_repo("slow-repo")
    response = client.post("/jobs", json={"source": {"url": str(repo)}})
    job_id = response.json()["job_id"]

    early = client.get(f"/jobs/{job_id}/wiki.zip")
    assert early.status_code == 404
    assert "not ready" in early.json()["detail"]

    assert wait_for_status(client, job_id)["status"] == "done"


def test_upload_zip_end_to_end(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("src/main.py", "print('hi')\n")
        zf.writestr("README.md", "# hi\n")
    buffer.seek(0)

    response = client.post(
        "/jobs/upload",
        files={"file": ("code.zip", buffer, "application/zip")},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"

    payload = wait_for_status(client, response.json()["job_id"])
    assert payload["status"] == "done", payload
    assert payload["source_type"] == "upload"
    assert payload["filename"] == "code.zip"

    download = client.get(f"/jobs/{payload['job_id']}/wiki.zip")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        assert ".openwiki/index.md" in set(zf.namelist())


def test_upload_rejects_unknown_extension(client):
    response = client.post(
        "/jobs/upload",
        files={"file": ("code.rar", io.BytesIO(b"not an archive"), "application/octet-stream")},
    )
    assert response.status_code == 422


def test_delete_job(client, make_git_repo):
    repo = make_git_repo("deletable-repo")
    response = client.post("/jobs", json={"source": {"url": str(repo)}})
    job_id = response.json()["job_id"]
    wait_for_status(client, job_id)

    assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_failed_job_can_be_retried(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_FAIL", "1")
    repo = make_git_repo("retry-repo")
    job_id = client.post("/jobs", json={"source": {"url": str(repo)}}).json()["job_id"]
    assert wait_for_status(client, job_id)["status"] == "failed"

    monkeypatch.delenv("FAKE_OPENWIKI_FAIL")
    retry = client.post(f"/jobs/{job_id}/retry")
    assert retry.status_code == 202
    assert retry.json()["status"] == "queued"

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "done", payload
    assert client.get(f"/jobs/{job_id}/wiki.zip").status_code == 200


def test_cancel_running_job_and_retry_resumes(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "10")
    repo = make_git_repo("cancel-running-repo")
    job_id = client.post("/jobs", json={"source": {"url": str(repo)}}).json()["job_id"]
    wait_for_state(client, job_id, "generating")

    cancel = client.post(f"/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelling"

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "cancelled"
    assert "cancelled" in (payload["error"] or "")

    retry = client.post(f"/jobs/{job_id}/retry")
    assert retry.status_code == 202
    assert wait_for_status(client, job_id, timeout=60)["status"] == "done"


def test_cancel_queued_job(client, make_git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_OPENWIKI_SLEEP", "3")
    first = client.post("/jobs", json={"source": {"url": str(make_git_repo("queued-a"))}}).json()["job_id"]
    wait_for_state(client, first, "generating")
    second = client.post("/jobs", json={"source": {"url": str(make_git_repo("queued-b"))}}).json()["job_id"]

    cancel = client.post(f"/jobs/{second}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
    assert client.get(f"/jobs/{second}").json()["status"] == "cancelled"

    assert wait_for_status(client, first)["status"] == "done"


def test_cancel_and_retry_validation(client, make_git_repo):
    repo = make_git_repo("cancel-validation-repo")
    job_id = client.post("/jobs", json={"source": {"url": str(repo)}}).json()["job_id"]
    wait_for_status(client, job_id)

    assert client.post(f"/jobs/{job_id}/cancel").status_code == 409
    assert client.post(f"/jobs/{job_id}/retry").status_code == 409
    assert client.post("/jobs/unknown/cancel").status_code == 404
    assert client.post("/jobs/unknown/retry").status_code == 404


def test_unknown_job_returns_404(client):
    assert client.get("/jobs/deadbeef").status_code == 404
    assert client.get("/jobs/..%2Fescape").status_code == 404


def test_token_requires_https_url(client):
    response = client.post(
        "/jobs",
        json={"source": {"url": "git@github.com:org/repo.git", "auth": {"token": "secret"}}},
    )
    assert response.status_code == 422


def _fake_args_from_zip(client, job_id: str) -> list[str]:
    download = client.get(f"/jobs/{job_id}/wiki.zip")
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        return json.loads(zf.read(".openwiki/.fake-args.json"))


def test_auto_mode_uses_update_when_the_source_ships_a_wiki(client, make_git_repo):
    repo = make_git_repo("wiki-repo", with_wiki=True)
    job_id = client.post("/jobs", json={"source": {"url": str(repo)}}).json()["job_id"]

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "done", payload
    assert payload["mode"] == "update"
    assert "--update" in _fake_args_from_zip(client, job_id)


def test_auto_mode_uses_init_without_a_wiki(client, make_git_repo):
    repo = make_git_repo("plain-repo")
    job_id = client.post("/jobs", json={"source": {"url": str(repo)}}).json()["job_id"]

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "done"
    assert payload["mode"] == "init"
    assert "--init" in _fake_args_from_zip(client, job_id)


def test_explicit_update_without_wiki_falls_back_to_init(client, make_git_repo):
    repo = make_git_repo("fallback-repo")
    body = {"source": {"url": str(repo)}, "mode": "update"}
    job_id = client.post("/jobs", json=body).json()["job_id"]

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "done"
    assert payload["mode"] == "init"
    logs = client.get(f"/jobs/{job_id}/logs", params={"tail": 50}).text
    assert "falling back to --init" in logs


def test_invalid_mode_is_rejected(client, make_git_repo):
    repo = make_git_repo("bad-mode-repo")
    response = client.post("/jobs", json={"source": {"url": str(repo)}, "mode": "banana"})
    assert response.status_code == 422


def test_hidden_wiki_is_adopted_for_incremental_updates(client, make_git_repo):
    repo = make_git_repo("hidden-wiki-repo", with_wiki=True, wiki_dir=".openwiki")
    job_id = client.post("/jobs", json={"source": {"url": str(repo)}}).json()["job_id"]

    payload = wait_for_status(client, job_id)
    assert payload["status"] == "done", payload
    assert payload["mode"] == "update"
    assert "--update" in _fake_args_from_zip(client, job_id)

    logs = client.get(f"/jobs/{job_id}/logs", params={"tail": 50}).text
    assert "adopted .openwiki/ as openwiki/" in logs

    download = client.get(f"/jobs/{job_id}/wiki.zip")
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        assert ".openwiki/index.md" in set(zf.namelist())


def test_completed_jobs_survive_a_restart(settings, make_git_repo):
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = make_git_repo("persistent-repo")
    with TestClient(create_app(settings)) as first:
        response = first.post("/jobs", json={"source": {"url": str(repo)}})
        job_id = response.json()["job_id"]
        assert wait_for_status(first, job_id)["status"] == "done"

    with TestClient(create_app(settings)) as second:
        payload = second.get(f"/jobs/{job_id}").json()
        assert payload["status"] == "done"
        assert second.get(f"/jobs/{job_id}/wiki.zip").status_code == 200


def test_active_jobs_are_resumed_after_a_restart(settings, make_git_repo):
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = make_git_repo("resume-repo")
    with TestClient(create_app(settings)) as first:
        response = first.post("/jobs", json={"source": {"url": str(repo)}})
        job_id = response.json()["job_id"]
        assert wait_for_status(first, job_id)["status"] == "done"

    # Simulate a crash mid-generation: active status, no packaged wiki yet.
    meta = settings.data_dir / "jobs" / job_id / "job.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["status"] = "generating"
    data["finished_at"] = None
    meta.write_text(json.dumps(data), encoding="utf-8")
    (settings.data_dir / "jobs" / job_id / "wiki.zip").unlink()

    with TestClient(create_app(settings)) as second:
        payload = wait_for_status(second, job_id)
        assert payload["status"] == "done", payload
        assert second.get(f"/jobs/{job_id}/wiki.zip").status_code == 200
