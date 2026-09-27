"""Application settings loaded from environment variables / .env."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    """``/data`` in containers, ``./data`` for local development on Windows."""
    if os.name == "nt":
        return Path.cwd() / "data"
    return Path("/data")


class Settings(BaseSettings):
    """Runtime configuration.

    Provider credentials (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ...) are not
    declared here on purpose: they stay in the process environment and are
    forwarded to the OpenWiki subprocess untouched.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- OpenWiki invocation ------------------------------------------------
    openwiki_bin: str = "openwiki"
    openwiki_config_dir: Path | None = None

    # --- Storage ------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)

    # --- Job limits ---------------------------------------------------------
    job_timeout_minutes: int = 45
    max_upload_mb: int = 200
    max_extracted_mb: int = 1024
    default_page_concurrency: int = 2
    log_tail_default: int = 50
    #: 0 keeps finished wikis forever; >0 deletes them after N hours on startup.
    job_retention_hours: int = 0

    # --- Worker pool --------------------------------------------------------
    #: Run an in-process worker (single container / dev / tests). Worker
    #: containers do not set this: they run ``python -m app.worker``.
    run_local_worker: bool = False
    #: Worker identity in the pool; defaults to the container hostname.
    worker_id: str = ""
    #: Generations a single worker runs in parallel (scale with replicas instead).
    max_concurrent_jobs: int = 1
    #: Seconds between claim attempts when the queue is empty.
    worker_poll_seconds: float = 2.0
    #: Seconds between progress/heartbeat updates while a generation runs.
    worker_progress_seconds: float = 2.0
    #: A claim with no heartbeat for this long is requeued by the API.
    worker_lease_seconds: int = 180
    #: Seconds between stale-claim sweeps in the API.
    worker_reap_seconds: int = 30

    # --- Model health check -------------------------------------------------
    #: Cached /health/model results; 0 disables the cache and probes every call.
    model_check_ttl_seconds: int = 30
    model_check_timeout_seconds: int = 20

    # --- OpenAI-compatible gateway helpers ----------------------------------
    #: Base URL as configured for OpenWiki (the proxy forwards to this origin).
    openai_compatible_base_url: str | None = None
    #: JSON object with extra headers for every OpenAI-compatible request,
    #: e.g. {"x-opencode-session": "openwiki-service"}.
    openai_compatible_extra_headers: str | None = None
    #: Loopback port of the header-injecting proxy (0 picks a free port).
    compat_proxy_port: int = 9100

    # --- Security -----------------------------------------------------------
    #: Allow cloning from local paths / file:// URLs. Keep disabled in production.
    allow_local_git: bool = False

    # --- Push back to the source repository ---------------------------------
    #: Author identity of the commits created when a job pushes the wiki.
    push_author_name: str = "openwiki-service"
    push_author_email: str = "openwiki-service@localhost"

    # --- Wiki artifact ------------------------------------------------------
    #: Folder name used inside the downloadable wiki.zip. OpenWiki always writes
    #: to "openwiki/" in the workspace; this only renames the packaged output.
    wiki_artifact_dir: str = ".openwiki"

    @field_validator("default_page_concurrency")
    @classmethod
    def _check_page_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("default_page_concurrency must be between 1 and 8")
        return value

    @field_validator(
        "job_timeout_minutes",
        "max_upload_mb",
        "max_concurrent_jobs",
        "model_check_timeout_seconds",
        "worker_lease_seconds",
        "worker_reap_seconds",
    )
    @classmethod
    def _check_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("value must be positive")
        return value

    @field_validator("worker_poll_seconds", "worker_progress_seconds")
    @classmethod
    def _check_positive_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("worker intervals must be positive")
        return value

    @field_validator("compat_proxy_port")
    @classmethod
    def _check_proxy_port(cls, value: int) -> int:
        if not 0 <= value <= 65535:
            raise ValueError("compat_proxy_port must be between 0 and 65535")
        return value

    @field_validator("wiki_artifact_dir")
    @classmethod
    def _check_artifact_dir(cls, value: str) -> str:
        value = value.strip()
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("wiki_artifact_dir must be a simple folder name")
        return value

    @field_validator("push_author_name", "push_author_email")
    @classmethod
    def _check_push_author(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("push author name/email must not be empty")
        return value

    @property
    def resolved_config_dir(self) -> Path:
        """OpenWiki state directory (credentials, install id, ...)."""
        return self.openwiki_config_dir or (self.data_dir / ".openwiki-config")

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"
