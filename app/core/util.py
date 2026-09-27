"""Helpers to keep credentials out of URLs, logs and error messages."""

from __future__ import annotations

import re
from typing import Iterable

_CREDENTIALS_IN_URL = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^/@\s:]+):(?P<password>[^/@\s]+)@"
)
_AUTH_HEADER = re.compile(r"(?i)((?:authorization|private-token)\s*[:=]\s*)(\S+)")


def redact_url(url: str) -> str:
    """Mask ``user:password@`` credentials embedded in a URL."""
    return _CREDENTIALS_IN_URL.sub(r"\g<scheme>\g<user>:***@", url or "")


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask URL credentials, auth headers and any known secret in free text."""
    out = _CREDENTIALS_IN_URL.sub(r"\g<scheme>\g<user>:***@", text or "")
    out = _AUTH_HEADER.sub(r"\1***", out)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out
