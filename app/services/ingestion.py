"""Fetch repository sources into a job workspace.

Handles three input shapes:

* public git URLs,
* private git URLs authenticated with a personal access token,
* uploaded zip/tar archives (the caller streams the upload to disk first).

Uploaded sources without ``.git`` are turned into a git repository with an
initial commit because OpenWiki versions its source evidence through git.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import shutil
import tarfile
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

from ..core.util import redact_text, redact_url

SUPPORTED_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)

_CLONE_TIMEOUT_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 300


class IngestionError(Exception):
    """Raised when a repository cannot be fetched or extracted safely."""


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------
def validate_git_url(url: str, *, allow_local: bool) -> str:
    """Validate a git URL and return it unchanged. Raises IngestionError."""
    url = (url or "").strip()
    if not url:
        raise IngestionError("source.url must not be empty")
    if url.startswith("git@"):  # scp-like SSH URL (git@host:org/repo.git)
        return url
    if re.match(r"^[A-Za-z]:[\\/]", url):  # Windows local path (C:\...)
        if allow_local and Path(url).exists():
            return url
        raise IngestionError("source.url must be an http(s) or ssh git URL")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"http", "https", "ssh", "git"}:
        if not parsed.netloc:
            raise IngestionError(f"invalid git URL: {redact_url(url)}")
        return url
    if scheme == "file":
        if not allow_local:
            raise IngestionError("file:// URLs are disabled (set ALLOW_LOCAL_GIT=true to enable)")
        return url
    if not scheme:
        if allow_local and Path(url).exists():
            return url
        raise IngestionError("source.url must be an http(s) or ssh git URL")
    raise IngestionError(f"unsupported URL scheme: {scheme}")


def token_basic_header(url: str, token: str) -> str:
    """Build an ``Authorization: Basic`` header for git over HTTPS.

    Each forge expects a specific username next to the token as password:
    GitHub ``x-access-token``, GitLab ``oauth2``, Bitbucket ``x-token-auth``.
    Anything else falls back to ``oauth2`` (Gitea, Forgejo, generic).
    """
    host = urlparse(url).netloc.lower()
    if "github" in host:
        username = "x-access-token"
    elif "gitlab" in host:
        username = "oauth2"
    elif "bitbucket" in host:
        username = "x-token-auth"
    else:
        username = "oauth2"
    credentials = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {credentials}"


def git_auth_prefix(url: str, token: str | None) -> list[str]:
    """``git -c`` prefix that authenticates http(s) requests with a token.

    Returns an empty list without a token (public repos, local paths).
    """
    if not token:
        return []
    if not url.lower().startswith(("http://", "https://")):
        raise IngestionError("auth.token is only supported for http(s) git URLs")
    return ["-c", f"http.extraHeader={token_basic_header(url, token)}"]


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------
async def run_git_status(
    args: list[str],
    *,
    cwd: Path | None = None,
    secrets: tuple[str, ...] = (),
    timeout_seconds: int = _GIT_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run git and return ``(exit_code, stdout, stderr)``, redacted.

    Unlike :func:`run_git` this does not raise on non-zero exits, so callers can
    use commands whose exit code carries meaning (``git diff --cached --quiet``).
    """
    # Note: ``safe.directory`` cannot be passed with ``-c`` (git only honours it
    # in system/global config), so the Docker image sets it system-wide. When
    # running outside Docker with repositories owned by another user, configure
    # ``git config --global --add safe.directory`` yourself.
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise IngestionError(f"git {args[0]} timed out after {timeout_seconds}s") from exc
    return (
        process.returncode or 0,
        redact_text(stdout.decode("utf-8", errors="replace"), secrets),
        redact_text(stderr.decode("utf-8", errors="replace"), secrets),
    )


async def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    secrets: tuple[str, ...] = (),
    timeout_seconds: int = _GIT_TIMEOUT_SECONDS,
) -> str:
    """Run git, raising :class:`IngestionError` on failure; returns stdout."""
    code, stdout, stderr = await run_git_status(
        args, cwd=cwd, secrets=secrets, timeout_seconds=timeout_seconds
    )
    if code != 0:
        detail = (stderr or stdout).strip()
        tail = "\n".join(detail.splitlines()[-8:])
        raise IngestionError(f"git {args[0]} failed (exit {code}):\n{tail}")
    return stdout


async def clone_git(
    url: str,
    dest: Path,
    *,
    ref: str | None = None,
    token: str | None = None,
) -> None:
    """Shallow-clone ``url`` into ``dest`` (dest must not exist yet)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    secrets = (token,) if token else ()
    prefix = git_auth_prefix(url, token)

    base = ["clone", "--depth", "1", "--no-tags"]
    if ref:
        try:
            await run_git(
                prefix + base + ["--branch", ref, "--single-branch", url, str(dest)],
                secrets=secrets,
                timeout_seconds=_CLONE_TIMEOUT_SECONDS,
            )
            return
        except IngestionError:
            # ``--branch`` cannot check out commit SHAs; fall back to fetch.
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)

    await run_git(prefix + base + [url, str(dest)], secrets=secrets, timeout_seconds=_CLONE_TIMEOUT_SECONDS)

    if ref:
        await run_git(
            prefix + ["fetch", "--depth", "1", "origin", ref],
            cwd=dest,
            secrets=secrets,
            timeout_seconds=_CLONE_TIMEOUT_SECONDS,
        )
        await run_git(prefix + ["checkout", "--quiet", "FETCH_HEAD"], cwd=dest, secrets=secrets)


async def ensure_full_history(repo_dir: Path, *, url: str, token: str | None = None) -> bool:
    """Turn a shallow clone into a full one (``openwiki --update`` needs it).

    ``--update`` diffs the current HEAD against the commit recorded in
    ``openwiki/.last-update.json``; a ``--depth 1`` clone does not contain that
    commit, so without this the update would see an empty change summary.
    Returns True when a fetch was performed.
    """
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git" / "shallow").exists():
        return False
    await run_git(
        [*git_auth_prefix(url, token), "fetch", "--quiet", "--unshallow", "origin"],
        cwd=repo_dir,
        secrets=(token,) if token else (),
        timeout_seconds=_CLONE_TIMEOUT_SECONDS,
    )
    return True


async def ensure_git_repo(repo_dir: Path, *, timeout_seconds: int = _GIT_TIMEOUT_SECONDS) -> bool:
    """Turn a plain directory into a git repository with an initial commit.

    Returns True when a repository was created, False when one already existed.
    """
    repo_dir = Path(repo_dir)
    if (repo_dir / ".git").exists():
        return False
    ident = ["-c", "user.email=openwiki-service@localhost", "-c", "user.name=openwiki-service"]
    await run_git(["init", "--quiet"], cwd=repo_dir, timeout_seconds=timeout_seconds)
    await run_git([*ident, "add", "--all"], cwd=repo_dir, timeout_seconds=timeout_seconds)
    await run_git(
        [*ident, "commit", "--quiet", "--allow-empty", "-m", "Import source for OpenWiki"],
        cwd=repo_dir,
        timeout_seconds=timeout_seconds,
    )
    return True


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
async def save_upload(chunks: AsyncIterator[bytes], dest: Path, *, max_bytes: int) -> tuple[int, str]:
    """Stream uploaded chunks to ``dest`` enforcing a size limit.

    Returns ``(bytes_written, sha256_hex)``; the digest identifies identical
    uploads so the API can deduplicate them.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    digest = hashlib.sha256()
    with dest.open("wb") as handle:
        async for chunk in chunks:
            written += len(chunk)
            if written > max_bytes:
                handle.close()
                dest.unlink(missing_ok=True)
                raise IngestionError(f"upload exceeds the {max_bytes // (1024 * 1024)} MB limit")
            digest.update(chunk)
            handle.write(chunk)
    return written, digest.hexdigest()


def archive_suffix(filename: str) -> str | None:
    """Return the matching supported archive suffix, longest first."""
    lowered = filename.lower()
    for suffix in sorted(SUPPORTED_ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(suffix):
            return suffix
    return None


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------
def extract_archive(archive: Path, dest: Path, *, max_extracted_mb: int) -> Path:
    """Safely extract a zip/tar archive into ``dest`` and return ``dest``.

    Rejects path traversal, absolute paths, symlinks/hardlinks and archives
    whose uncompressed size exceeds ``max_extracted_mb``.
    """
    archive = Path(archive)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    max_bytes = max_extracted_mb * 1024 * 1024
    name = archive.name.lower()

    if name.endswith(".zip"):
        _extract_zip(archive, dest, max_bytes)
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        _extract_tar(archive, dest, max_bytes)
    else:
        raise IngestionError(f"unsupported archive type: {archive.name}")

    _normalize_single_root(dest)
    return dest


def _check_member_path(dest_root: Path, member_name: str) -> None:
    name = (member_name or "").replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise IngestionError(f"unsafe path in archive: {member_name!r}")
    parts = Path(name).parts
    if ".." in parts:
        raise IngestionError(f"unsafe path in archive: {member_name!r}")
    target = (dest_root / name).resolve()
    if dest_root.resolve() not in target.parents and target != dest_root.resolve():
        raise IngestionError(f"unsafe path in archive: {member_name!r}")


def _extract_zip(archive: Path, dest: Path, max_bytes: int) -> None:
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            _check_member_path(dest, info.filename)
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise IngestionError(f"symlink entries are not allowed: {info.filename!r}")
            total += info.file_size
            if total > max_bytes:
                raise IngestionError(f"archive expands beyond the {max_bytes // (1024 * 1024)} MB limit")
        zf.extractall(dest)


def _extract_tar(archive: Path, dest: Path, max_bytes: int) -> None:
    total = 0
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        for member in members:
            _check_member_path(dest, member.name)
            if member.issym() or member.islnk():
                raise IngestionError(f"link entries are not allowed: {member.name!r}")
            total += member.size
            if total > max_bytes:
                raise IngestionError(f"archive expands beyond the {max_bytes // (1024 * 1024)} MB limit")
        try:
            tf.extractall(dest, members=members, filter="data")
        except TypeError:  # Python < 3.11.4 has no extraction filters
            tf.extractall(dest, members=members)


def _normalize_single_root(dest: Path) -> None:
    """Move the contents of a single top-level folder up into ``dest``."""
    entries = list(dest.iterdir())
    if len(entries) != 1:
        return
    root = entries[0]
    if not root.is_dir() or root.name == ".git":
        return
    for child in list(root.iterdir()):
        child.rename(dest / child.name)
    root.rmdir()
