"""Stable fingerprints used to deduplicate identical wiki requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse


def canonical_git_url(url: str) -> str:
    """Normalize an http(s) git URL so ``repo``, ``repo.git`` and ``repo/`` collide."""
    url = (url or "").strip()
    if url.lower().startswith(("http://", "https://")):
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").rstrip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        return f"{parsed.scheme.lower()}://{host}{path}"
    return url


def wiki_fingerprint(
    *,
    source: dict[str, Any],
    language: str | None,
    mode: str,
    push: dict[str, Any] | None,
) -> str:
    """Hash the request fields that identify the same generation.

    Repository contents and commits are *not* part of the fingerprint: while a
    wiki is queued or running for a target, submitting it again reuses it. The
    ``force`` flag on the API bypasses this check.
    """
    if source.get("type") == "upload":
        identity: dict[str, Any] = {"type": "upload", "sha256": source.get("sha256")}
    else:
        identity = {
            "type": "git",
            "url": canonical_git_url(str(source.get("url") or "")),
            "ref": source.get("ref") or None,
        }
    push_summary = None
    if isinstance(push, dict):
        push_summary = {
            "enabled": bool(push.get("enabled", True)),
            "branch": push.get("branch") or None,
        }
    payload = {
        "source": identity,
        "language": (language or "").strip().lower() or None,
        "mode": mode,
        "push": push_summary,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
