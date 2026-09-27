"""Unit tests for source ingestion helpers."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tarfile
import zipfile

import pytest

from app.services import ingestion


def test_validate_git_url_accepts_http_and_ssh():
    assert ingestion.validate_git_url("https://github.com/org/repo.git", allow_local=False)
    assert ingestion.validate_git_url("git@github.com:org/repo.git", allow_local=False)
    assert ingestion.validate_git_url("ssh://git@host/org/repo.git", allow_local=False)


def test_validate_git_url_rejects_unknown_scheme():
    with pytest.raises(ingestion.IngestionError):
        ingestion.validate_git_url("ftp://example.com/repo.git", allow_local=False)


def test_validate_git_url_local_paths_are_opt_in():
    with pytest.raises(ingestion.IngestionError):
        ingestion.validate_git_url("/some/local/path", allow_local=False)


def test_archive_suffix_matches_longest():
    assert ingestion.archive_suffix("code.tar.gz") == ".tar.gz"
    assert ingestion.archive_suffix("code.zip") == ".zip"
    assert ingestion.archive_suffix("code.rar") is None


def test_token_basic_header_per_forge():
    header = ingestion.token_basic_header("https://github.com/org/repo.git", "tok")
    assert header.startswith("Authorization: Basic ")
    assert ingestion.token_basic_header("https://gitlab.com/org/repo.git", "tok").startswith("Authorization: Basic ")


def test_extract_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "boom")
    with pytest.raises(ingestion.IngestionError):
        ingestion.extract_archive(archive, tmp_path / "out", max_extracted_mb=10)


def test_extract_zip_rejects_absolute_path(tmp_path):
    archive = tmp_path / "absolute.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "boom")
    with pytest.raises(ingestion.IngestionError):
        ingestion.extract_archive(archive, tmp_path / "out", max_extracted_mb=10)


def test_extract_zip_enforces_size_limit(tmp_path):
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("big.txt", "x" * (2 * 1024 * 1024))
    with pytest.raises(ingestion.IngestionError):
        ingestion.extract_archive(archive, tmp_path / "out", max_extracted_mb=1)


def test_extract_zip_normalizes_single_root_folder(tmp_path):
    archive = tmp_path / "code.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("project/README.md", "hi")
        zf.writestr("project/src/main.py", "print(1)\n")
    root = ingestion.extract_archive(archive, tmp_path / "out", max_extracted_mb=10)
    assert (root / "README.md").exists()
    assert (root / "src" / "main.py").exists()


def test_extract_tar_rejects_symlinks(tmp_path):
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ingestion.IngestionError):
        ingestion.extract_archive(archive, tmp_path / "out", max_extracted_mb=10)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_ensure_git_repo_initializes_and_is_idempotent(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    assert asyncio.run(ingestion.ensure_git_repo(tmp_path)) is True
    assert (tmp_path / ".git").exists()
    assert asyncio.run(ingestion.ensure_git_repo(tmp_path)) is False


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=tests@example.com", "-c", "user.name=tests", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_source_repo(tmp_path):
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("main\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "main")
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_clone_git_checks_out_branch(tmp_path):
    source = _make_source_repo(tmp_path)
    default_branch = _git(source, "rev-parse", "--abbrev-ref", "HEAD")
    _git(source, "checkout", "--quiet", "-b", "feature")
    (source / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "feature")
    _git(source, "checkout", "--quiet", default_branch)

    dest = tmp_path / "clone-branch"
    asyncio.run(ingestion.clone_git(str(source), dest, ref="feature"))
    assert (dest / "feature.txt").exists()
    assert (dest / "README.md").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_clone_git_checks_out_commit_sha(tmp_path):
    source = _make_source_repo(tmp_path)
    sha = _git(source, "rev-parse", "HEAD")
    # Allow fetching a raw commit SHA from the local source repository.
    _git(source, "config", "uploadpack.allowAnySHA1InWant", "true")

    dest = tmp_path / "clone-sha"
    asyncio.run(ingestion.clone_git(str(source), dest, ref=sha))
    assert (dest / "README.md").exists()
