"""Push-back tests: commit the generated wiki to the source repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ingestion import IngestionError, git_auth_prefix
from tests.conftest import _git, wait_for_status


def _remote_names(origin: Path, branch: str = "main") -> set[str]:
    return set(_git(["ls-tree", "-r", "--name-only", branch], origin).splitlines())


def test_git_auth_prefix():
    assert git_auth_prefix("https://github.com/acme/repo.git", None) == []
    prefix = git_auth_prefix("https://github.com/acme/repo.git", "token-123")
    assert prefix[0] == "-c"
    assert prefix[1].startswith("http.extraHeader=Authorization: Basic ")
    with pytest.raises(IngestionError):
        git_auth_prefix("git@github.com:acme/repo.git", "token-123")


def test_push_commits_the_wiki_to_the_cloned_branch(client, make_remote_repo):
    origin = make_remote_repo("pushed")
    body = {"source": {"url": origin.as_uri()}, "push": {}}
    wiki_id = client.post("/wikis", json=body).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done", payload
    assert payload["push"]["enabled"] is True
    result = payload["push_result"]
    assert result["status"] == "pushed"
    assert result["branch"] == "main"
    assert len(result["commit"]) == 40

    names = _remote_names(origin)
    assert ".openwiki/index.md" in names
    assert ".openwiki/architecture.md" in names
    assert any(name.startswith(".openwiki/.claims/") for name in names)
    # Transient resume state and the scaffolding outside the wiki are not committed.
    assert ".openwiki/.run.json" not in names
    assert "AGENTS.md" not in names

    assert _git(["log", "-1", "--format=%s", result["commit"]], origin).strip() == "docs: generate OpenWiki wiki"
    assert (
        _git(["log", "-1", "--format=%an <%ae>", result["commit"]], origin).strip()
        == "openwiki-service <openwiki-service@localhost>"
    )

    logs = client.get(f"/wikis/{wiki_id}/logs", params={"tail": 50}).text
    assert "push: pushed (branch main)" in logs


def test_push_to_a_custom_branch(client, make_remote_repo):
    origin = make_remote_repo("branch-push")
    body = {
        "source": {"url": origin.as_uri()},
        "push": {"branch": "openwiki/update", "message": "docs: wiki from CI"},
    }
    wiki_id = client.post("/wikis", json=body).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    result = payload["push_result"]
    assert result["status"] == "pushed"
    assert result["branch"] == "openwiki/update"
    assert _git(["rev-parse", "--verify", "openwiki/update"], origin).strip() == result["commit"]
    # The branch that was cloned is untouched.
    assert _git(["rev-parse", "main"], origin).strip() != result["commit"]
    assert _git(["log", "-1", "--format=%s", "openwiki/update"], origin).strip() == "docs: wiki from CI"


def test_push_reports_no_changes_when_the_wiki_is_identical(client, make_remote_repo):
    origin = make_remote_repo("no-change-push")
    first = client.post("/wikis", json={"source": {"url": origin.as_uri()}, "push": {}}).json()["wiki_id"]
    assert wait_for_status(client, first)["push_result"]["status"] == "pushed"
    head = _git(["rev-parse", "main"], origin).strip()

    # Same CLI arguments and environment, so the fake wiki is byte-identical.
    body = {"source": {"url": origin.as_uri()}, "mode": "init", "push": {}}
    second = client.post("/wikis", json=body).json()["wiki_id"]
    payload = wait_for_status(client, second)
    assert payload["status"] == "done", payload
    assert payload["push_result"]["status"] == "no_changes"
    assert _git(["rev-parse", "main"], origin).strip() == head


def test_push_requires_a_token_for_https(client):
    body = {"source": {"url": "https://github.com/acme/repo.git"}, "push": {}}
    response = client.post("/wikis", json=body)
    assert response.status_code == 422
    assert "auth.token" in response.json()["detail"]


def test_push_rejects_ssh_sources(client):
    body = {"source": {"url": "git@github.com:acme/repo.git"}, "push": {}}
    response = client.post("/wikis", json=body)
    assert response.status_code == 422
    assert "http(s)" in response.json()["detail"]


def test_push_with_an_invalid_branch_is_rejected(client, make_remote_repo):
    origin = make_remote_repo("bad-branch-push")
    body = {"source": {"url": origin.as_uri()}, "push": {"branch": "bad..branch"}}
    assert client.post("/wikis", json=body).status_code == 422


def test_push_failure_keeps_the_wiki_downloadable(client, make_remote_repo):
    # A non-bare origin with the branch checked out refuses the push.
    origin = make_remote_repo("denied-push", bare=False)
    body = {"source": {"url": origin.as_uri()}, "push": {}}
    wiki_id = client.post("/wikis", json=body).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "failed"
    assert "push failed" in (payload["error"] or "")
    assert payload["push_result"]["status"] == "failed"
    assert client.get(f"/wikis/{wiki_id}/download").status_code == 200


def test_push_of_a_detached_checkout_needs_a_branch(client, make_remote_repo):
    origin = make_remote_repo("detached-push")
    _git(["tag", "v1", "main"], origin)
    body = {"source": {"url": origin.as_uri(), "ref": "v1"}, "push": {}}
    wiki_id = client.post("/wikis", json=body).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "failed"
    assert "push.branch is required" in (payload["error"] or "")


def test_disabled_push_is_skipped(client, make_remote_repo):
    origin = make_remote_repo("skipped-push")
    head = _git(["rev-parse", "main"], origin).strip()
    body = {"source": {"url": origin.as_uri()}, "push": {"enabled": False}}
    wiki_id = client.post("/wikis", json=body).json()["wiki_id"]

    payload = wait_for_status(client, wiki_id)
    assert payload["status"] == "done"
    assert payload["push_result"]["status"] == "skipped"
    assert _git(["rev-parse", "main"], origin).strip() == head


def test_update_wikis_fetch_the_full_history(client, settings, make_remote_repo):
    origin = make_remote_repo("history-push")
    first = client.post("/wikis", json={"source": {"url": origin.as_uri()}, "push": {}}).json()["wiki_id"]
    assert wait_for_status(client, first)["status"] == "done"

    body = {"source": {"url": origin.as_uri()}, "push": {}}
    second = client.post("/wikis", json=body).json()["wiki_id"]
    payload = wait_for_status(client, second)
    assert payload["status"] == "done", payload
    assert payload["mode"] == "update"

    shallow = settings.data_dir / "jobs" / second / "repo" / ".git" / "shallow"
    assert not shallow.exists()
    logs = client.get(f"/wikis/{second}/logs", params={"tail": 50}).text
    assert "fetched the full history" in logs
