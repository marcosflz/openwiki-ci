"""Request/response models exposed through OpenAPI."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .core.util import redact_url


class WikiStatus(str, Enum):
    queued = "queued"
    fetching = "fetching"
    generating = "generating"
    finalizing = "finalizing"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class GitAuth(BaseModel):
    token: str = Field(
        ...,
        min_length=1,
        description="Personal access token (GitHub/GitLab/Bitbucket) for private repositories.",
    )


class GitSource(BaseModel):
    type: Literal["git"] = "git"
    url: str = Field(..., description="HTTPS, SSH or local (when enabled) Git URL.")
    ref: str | None = Field(None, description="Branch, tag or commit to check out.")
    auth: GitAuth | None = None


_BRANCH_FORBIDDEN_CHARS = " ~^:?*[\\"


class PushOptions(BaseModel):
    """Commit the generated wiki and push it back to the source repository."""

    enabled: bool = Field(True, description="Set false to keep the configuration but skip pushing.")
    branch: str | None = Field(
        None,
        max_length=255,
        description=(
            "Target branch; defaults to the branch that was cloned. Use e.g. "
            "'openwiki/update' to push a review branch instead of committing to the current one."
        ),
    )
    message: str | None = Field(
        None,
        max_length=500,
        description="Commit message; defaults to 'docs: generate/update OpenWiki wiki'.",
    )

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if (
            not value
            or value.startswith(("-", "/"))
            or value.endswith(("/", "."))
            or ".." in value
            or "@{" in value
            or "//" in value
            or any(char in value for char in _BRANCH_FORBIDDEN_CHARS)
        ):
            raise ValueError("push.branch is not a valid git branch name")
        return value

    @field_validator("message")
    @classmethod
    def _check_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("push.message must not be empty")
        return value


class PushResult(BaseModel):
    """Outcome of the push requested for a wiki."""

    status: Literal["pushed", "no_changes", "failed", "skipped"]
    branch: str | None = None
    commit: str | None = None
    detail: str | None = None
    at: str | None = None


class WikiCreate(BaseModel):
    """Input of ``POST /wikis``: where to get the code and how to document it."""

    source: GitSource
    language: str | None = Field(
        None,
        min_length=2,
        max_length=32,
        description='Write the wiki pages in this language, e.g. "es". Creates openwiki/INSTRUCTIONS.md.',
    )
    concurrency: int | None = Field(
        None,
        ge=1,
        le=8,
        description="OpenWiki page workers for this generation (OPENWIKI_PAGE_CONCURRENCY).",
    )
    mode: Literal["auto", "init", "update"] = Field(
        "auto",
        description=(
            "auto: --update when the source ships openwiki/ docs, --init otherwise. "
            "init regenerates the whole wiki; update only rechecks changes and stale claims."
        ),
    )
    push: PushOptions | None = Field(
        None,
        description=(
            "Push the generated wiki back to the source repository. Requires an "
            "http(s) URL with source.auth.token (PAT with write access)."
        ),
    )


class WikiAccepted(BaseModel):
    wiki_id: str
    status: WikiStatus
    links: dict[str, str]
    deduplicated: bool = Field(
        False,
        description=(
            "True when an identical wiki was already queued or running and it was "
            "reused instead of creating a new one (submit with ?force=true to override)."
        ),
    )


class WikiView(BaseModel):
    wiki_id: str
    status: WikiStatus
    source_type: str
    source_url: str = Field("", description="Redacted URL for git sources.")
    ref: str | None = None
    filename: str | None = None
    language: str | None = None
    concurrency: int | None = None
    mode: str = "auto"
    resolved_mode: str | None = Field(
        None, description="Mode actually executed (auto may resolve to init or update)."
    )
    push: PushOptions | None = None
    push_result: PushResult | None = None
    worker: str | None = Field(None, description="Worker that claimed the generation.")
    attempts: int = 0
    progress: str | None = None
    pages: int | None = None
    size_bytes: int | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


def wiki_to_view(job: dict[str, Any]) -> WikiView:
    """Map an internal job record to the public wiki representation.

    Internally a generation is a ``job`` (queue, workers, ``job.json``); the
    public API exposes it as a ``wiki``.
    """
    source = job.get("source") or {}
    source_type = source.get("type", "git")
    if source_type == "git":
        source_url = redact_url(source.get("url", ""))
        ref = source.get("ref")
        filename = None
    else:
        source_url = ""
        ref = None
        filename = source.get("filename")
    push_request = job.get("push")
    push_result_data = job.get("push_result")
    return WikiView(
        wiki_id=job["wiki_id"],
        status=WikiStatus(job.get("status", "queued")),
        source_type=source_type,
        source_url=source_url,
        ref=ref,
        filename=filename,
        language=job.get("language"),
        concurrency=job.get("concurrency"),
        mode=job.get("mode", "auto"),
        resolved_mode=job.get("resolved_mode"),
        push=PushOptions(**push_request) if isinstance(push_request, dict) else None,
        push_result=PushResult(**push_result_data) if isinstance(push_result_data, dict) else None,
        worker=job.get("claimed_by"),
        attempts=int(job.get("attempts") or 0),
        progress=job.get("progress"),
        pages=job.get("pages"),
        size_bytes=job.get("size_bytes"),
        error=job.get("error"),
        created_at=job.get("created_at"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )
