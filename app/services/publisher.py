"""Commit the generated wiki and push it back to the source repository."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from ..core.storage import utc_now
from ..core.util import redact_url
from .ingestion import IngestionError, git_auth_prefix, run_git, run_git_status
from .wiki_runner import NATIVE_WIKI_DIR

#: Commit messages used when the job does not provide one.
DEFAULT_MESSAGES = {
    "init": "docs: generate OpenWiki wiki",
    "update": "docs: update OpenWiki wiki",
}
_PUSH_TIMEOUT_SECONDS = 300
_REMOTE_LOOKUP_TIMEOUT_SECONDS = 60


class PushError(Exception):
    """Raised when the generated wiki cannot be committed or pushed."""


async def publish_wiki(
    repo_dir: Path,
    *,
    artifact_dir: str,
    url: str,
    token: str | None,
    mode: str,
    options: dict,
    author_name: str,
    author_email: str,
    log: Callable[[str], None],
) -> dict:
    """Commit ``<artifact_dir>/`` and push it to ``url``.

    Returns the ``push_result`` recorded on the job. ``.run.json`` (transient
    resume state) is excluded from the commit, matching OpenWiki's own update
    workflow; the scaffolding outside the wiki folder is left untouched.
    """
    repo_dir = Path(repo_dir)
    secrets = (token,) if token else ()
    try:
        branch = await _target_branch(repo_dir, options.get("branch"))
        await _sync_with_remote(repo_dir, url, branch, token, secrets, log)
        _expose_artifact_dir(repo_dir, artifact_dir, log)

        await run_git(
            ["add", "--all", "--force", "--", artifact_dir, f":(exclude){artifact_dir}/.run.json"],
            cwd=repo_dir,
            secrets=secrets,
        )
        code, _, stderr = await run_git_status(["diff", "--cached", "--quiet"], cwd=repo_dir, secrets=secrets)
        if code > 1:
            raise PushError(f"git diff failed (exit {code}): {stderr.strip()}")
        if code == 1:
            message = str(
                options.get("message") or DEFAULT_MESSAGES.get(mode, "docs: update OpenWiki wiki")
            )
            await run_git(
                [
                    "-c",
                    f"user.name={author_name}",
                    "-c",
                    f"user.email={author_email}",
                    "commit",
                    "--quiet",
                    "--message",
                    message,
                ],
                cwd=repo_dir,
                secrets=secrets,
            )
            log(f"committed {artifact_dir}/: {message}")

        _, stdout, _ = await run_git_status(["rev-parse", "HEAD"], cwd=repo_dir, secrets=secrets)
        commit = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
        remote_commit = await _remote_commit(repo_dir, url, branch, token, secrets)
        if code == 0 and remote_commit == commit:
            return {
                "status": "no_changes",
                "branch": branch,
                "commit": commit,
                "detail": f"{artifact_dir}/ is already up to date on {branch}",
                "at": utc_now(),
            }

        log(f"pushing {artifact_dir}/ to {redact_url(url)} (branch {branch})")
        await run_git(
            [*git_auth_prefix(url, token), "push", "--quiet", url, f"HEAD:refs/heads/{branch}"],
            cwd=repo_dir,
            secrets=secrets,
            timeout_seconds=_PUSH_TIMEOUT_SECONDS,
        )
    except IngestionError as exc:
        raise PushError(str(exc)) from exc
    return {
        "status": "pushed",
        "branch": branch,
        "commit": commit,
        "detail": f"pushed to {branch}",
        "at": utc_now(),
    }


async def _sync_with_remote(
    repo_dir: Path,
    url: str,
    branch: str,
    token: str | None,
    secrets: tuple[str, ...],
    log: Callable[[str], None],
) -> None:
    """Fast-forward the workspace to the remote branch before committing.

    Updates may document remote commits newer than the clone (OpenWiki diffs
    against the fetched branch). Moving HEAD to the remote tip first keeps the
    wiki commit on top of the latest history, so the push is a fast-forward
    instead of a non-fast-forward rejection.
    """
    try:
        code, _, _ = await run_git_status(
            [*git_auth_prefix(url, token), "fetch", "--quiet", url, f"refs/heads/{branch}"],
            cwd=repo_dir,
            secrets=secrets,
            timeout_seconds=_REMOTE_LOOKUP_TIMEOUT_SECONDS,
        )
        if code != 0:
            return
        _, out, _ = await run_git_status(["rev-parse", "FETCH_HEAD"], cwd=repo_dir, secrets=secrets)
        remote_tip = out.strip().splitlines()[0].strip() if out.strip() else ""
        _, out, _ = await run_git_status(["rev-parse", "HEAD"], cwd=repo_dir, secrets=secrets)
        head = out.strip()
        if not remote_tip or not head or remote_tip == head:
            return
        code, _, _ = await run_git_status(
            ["merge", "--ff-only", "--quiet", remote_tip], cwd=repo_dir, secrets=secrets
        )
    except IngestionError:
        return
    if code == 0:
        log(f"fast-forwarded the workspace to {remote_tip[:12]} before pushing")


async def _target_branch(repo_dir: Path, requested: str | None) -> str:
    """Resolve the branch to push to (defaults to the cloned branch)."""
    if requested:
        branch = requested
    else:
        code, stdout, _ = await run_git_status(["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo_dir)
        if code != 0 or not stdout.strip():
            raise PushError("push.branch is required when the source is checked out at a tag or commit")
        branch = stdout.strip()
    code, _, stderr = await run_git_status(["check-ref-format", "--branch", branch], cwd=repo_dir)
    if code != 0:
        raise PushError(f"invalid push.branch {branch!r}: {stderr.strip()}")
    return branch


def _expose_artifact_dir(repo_dir: Path, artifact_dir: str, log: Callable[[str], None]) -> None:
    """Rename OpenWiki's ``openwiki/`` to ``artifact_dir/`` before committing."""
    native = repo_dir / NATIVE_WIKI_DIR
    if not native.is_dir():
        raise PushError("openwiki/ directory is missing; nothing to push")
    if artifact_dir == NATIVE_WIKI_DIR:
        return
    target = repo_dir / artifact_dir
    if target.exists():
        log(f"replacing the existing {artifact_dir}/ tree with the updated wiki")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    native.rename(target)


async def _remote_commit(
    repo_dir: Path,
    url: str,
    branch: str,
    token: str | None,
    secrets: tuple[str, ...],
) -> str | None:
    """Current commit of the remote branch, or None when unknown/missing."""
    try:
        code, stdout, _ = await run_git_status(
            [*git_auth_prefix(url, token), "ls-remote", "--quiet", url, f"refs/heads/{branch}"],
            cwd=repo_dir,
            secrets=secrets,
            timeout_seconds=_REMOTE_LOOKUP_TIMEOUT_SECONDS,
        )
    except IngestionError:
        return None
    if code != 0 or not stdout.strip():
        return None
    return stdout.split()[0].strip()
