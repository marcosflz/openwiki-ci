"""Shared fixtures for the test suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FAKE_OPENWIKI = FIXTURES_DIR / "fake_openwiki.py"


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(args: list[str], cwd: Path) -> str:
    """Run git in ``cwd`` and return stdout (raises on failure)."""
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout


def _write_sample_files(repo: Path, *, with_wiki: bool, wiki_dir: str) -> None:
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    if with_wiki:
        wiki = repo / wiki_dir
        (wiki / ".claims").mkdir(parents=True, exist_ok=True)
        (wiki / "index.md").write_text(
            '---\ntype: index\nokf_version: "0.2"\n---\n\n# Existing wiki\n',
            encoding="utf-8",
        )
        (wiki / ".last-update.json").write_text('{"status": "success"}', encoding="utf-8")


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Service settings pointing OPENWIKI_BIN at the fake CLI."""
    return Settings(
        data_dir=tmp_path / "data",
        openwiki_bin=f'"{sys.executable}" "{FAKE_OPENWIKI}"',
        allow_local_git=True,
        max_concurrent_jobs=1,
        job_timeout_minutes=2,
        default_page_concurrency=1,
    )


@pytest.fixture()
def client(settings: Settings):
    from fastapi.testclient import TestClient

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def make_git_repo(tmp_path: Path):
    """Create a small local git repository with one commit."""

    def _make(
        name: str = "sample-repo",
        *,
        with_wiki: bool = False,
        wiki_dir: str = "openwiki",
    ) -> Path:
        if not git_available():
            pytest.skip("git is required for this test")
        repo = tmp_path / name
        _write_sample_files(repo, with_wiki=with_wiki, wiki_dir=wiki_dir)
        _git(["init", "--quiet"], repo)
        _git(["-c", "user.email=tests@example.com", "-c", "user.name=tests", "add", "--all"], repo)
        _git(
            ["-c", "user.email=tests@example.com", "-c", "user.name=tests", "commit", "--quiet", "-m", "init"],
            repo,
        )
        return repo

    return _make


@pytest.fixture()
def make_remote_repo(tmp_path: Path):
    """Create a repository usable as a push target.

    ``bare=True`` (default) returns a bare repository that accepts pushes.
    ``bare=False`` returns the non-bare seed with its branch checked out, where
    git refuses pushes to the current branch (usable to test push failures).
    """

    def _make(
        name: str = "remote",
        *,
        with_wiki: bool = False,
        wiki_dir: str = ".openwiki",
        branch: str = "main",
        bare: bool = True,
    ) -> Path:
        if not git_available():
            pytest.skip("git is required for this test")
        seed = tmp_path / f"{name}-seed"
        _write_sample_files(seed, with_wiki=with_wiki, wiki_dir=wiki_dir)
        _git(["init", "--quiet"], seed)
        _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], seed)
        _git(["-c", "user.email=tests@example.com", "-c", "user.name=tests", "add", "--all"], seed)
        _git(
            ["-c", "user.email=tests@example.com", "-c", "user.name=tests", "commit", "--quiet", "-m", "init"],
            seed,
        )
        if not bare:
            return seed
        origin = tmp_path / f"{name}.git"
        _git(["init", "--bare", "--quiet", str(origin)], tmp_path)
        _git(["push", "--quiet", str(origin), f"{branch}:{branch}"], seed)
        _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], origin)
        return origin

    return _make


def wait_for_status(client, wiki_id: str, *, timeout: float = 30.0) -> dict:
    """Poll a wiki until it reaches a terminal status."""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/wikis/{wiki_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"done", "failed", "cancelled"}:
            return last
        time.sleep(0.1)
    raise AssertionError(f"wiki {wiki_id} did not finish within {timeout}s (last={last})")


def wait_for_state(client, wiki_id: str, wanted: str, *, timeout: float = 15.0) -> dict:
    """Poll a wiki until it reaches a specific non-terminal status."""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/wikis/{wiki_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] == wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"wiki {wiki_id} never reached {wanted} (last={last})")
