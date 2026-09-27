"""Model provider status: passive configuration summary + active probe.

The passive part mirrors the environment OpenWiki reads (process environment
layered over ``<OPENWIKI_CONFIG_DIR>/.env``); it never contacts a model.

The active part sends the smallest possible completion request to the
configured provider to prove credentials and connectivity work end to end.
Providers whose auth is not a plain API key (Bedrock IAM, Copilot session,
ChatGPT login, Gemini Enterprise ADC) report ``unsupported`` instead of
guessing.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..core.config import Settings
from ..core.util import redact_text, redact_url
from .provider_proxy import parse_extra_headers

PROBE_PROMPT = "Reply with the single word: ok"
DEFAULT_PROVIDER = "openai"

#: Probe kinds: how the active check talks to a provider.
#: ``openai-responses`` -> POST /responses, ``anthropic`` -> /v1/messages,
#: ``gemini`` -> generateContent, ``chat`` -> /chat/completions.
@dataclass(frozen=True)
class ProviderSpec:
    credential_env: str | None = None
    base_url_env: str | None = None
    default_base_url: str | None = None
    default_model: str | None = None
    probe: str | None = None
    note: str | None = None


#: Mirrors the provider table from the OpenWiki README. Credential env names for
#: the OpenAI-compatible vendors are best effort.
PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1", "gpt-5.6-terra", "openai-responses"
    ),
    "anthropic": ProviderSpec(
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "https://api.anthropic.com", None, "anthropic"
    ),
    "gemini": ProviderSpec(
        "GEMINI_API_KEY", None, "https://generativelanguage.googleapis.com", None, "gemini"
    ),
    "openrouter": ProviderSpec(
        "OPENROUTER_API_KEY", None, "https://openrouter.ai/api/v1", None, "chat"
    ),
    "openai-compatible": ProviderSpec(
        "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL", None, None, "chat"
    ),
    "nvidia": ProviderSpec("NVIDIA_API_KEY", "NVIDIA_BASE_URL", None, None, "chat"),
    "fireworks": ProviderSpec("FIREWORKS_API_KEY", "FIREWORKS_BASE_URL", None, None, "chat"),
    "baseten": ProviderSpec("BASETEN_API_KEY", "BASETEN_BASE_URL", None, None, "chat"),
    "nebius": ProviderSpec(
        "NEBIUS_API_KEY", None, None, None, None, "active check needs a base URL and is not implemented yet"
    ),
    "bedrock": ProviderSpec(None, None, None, None, None, "uses IAM credentials instead of an API key"),
    "copilot": ProviderSpec(
        "COPILOT_API_KEY", "COPILOT_BASE_URL", None, None, None, "uses a GitHub OAuth token or gh session"
    ),
    "gemini-enterprise": ProviderSpec(None, None, None, None, None, "uses Google ADC instead of an API key"),
    "openai-chatgpt": ProviderSpec(None, None, None, None, None, "uses a browser sign-in session"),
}


class ProbeError(Exception):
    """The provider answered with an error or an unusable response."""


#: Paths that belong to the request URL, never to the base URL.
_ENDPOINT_SUFFIXES = ("/chat/completions", "/responses", "/completions", "/messages")


def _split_base_url(base_url: str) -> tuple[str, str | None]:
    """Split a base URL that wrongly includes an endpoint path.

    Returns ``(root, suffix)``; ``suffix`` is None when the URL looks like a
    proper API root.
    """
    trimmed = base_url.rstrip("/")
    lowered = trimmed.lower()
    for suffix in _ENDPOINT_SUFFIXES:
        if lowered.endswith(suffix):
            return trimmed[: len(trimmed) - len(suffix)], suffix
    return trimmed, None


# ---------------------------------------------------------------------------
# Configuration (passive)
# ---------------------------------------------------------------------------
def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` parser for OpenWiki's config directory .env."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_openwiki_env(settings: Settings) -> dict[str, str]:
    """Process environment layered over ``<config_dir>/.env`` (env wins)."""
    merged = _parse_env_file(settings.resolved_config_dir / ".env")
    merged.update(os.environ)
    return merged


def describe_model(settings: Settings) -> dict[str, Any]:
    """Passive snapshot of the model configuration, no secrets included."""
    file_env = _parse_env_file(settings.resolved_config_dir / ".env")
    env = {**file_env, **os.environ}

    def source_of(key: str | None, fallback: str | None = None) -> str | None:
        if not key:
            return fallback
        if key in os.environ:
            return "environment"
        if key in file_env:
            return "config"
        return fallback

    raw_provider = env.get("OPENWIKI_PROVIDER")
    provider = (raw_provider or DEFAULT_PROVIDER).strip().lower()
    spec = PROVIDERS.get(provider)

    model = env.get("OPENWIKI_MODEL_ID") or (spec.default_model if spec else None)
    credential_env = spec.credential_env if spec else None
    base_url = None
    if spec:
        raw_base = env.get(spec.base_url_env) if spec.base_url_env else None
        base_url = raw_base or spec.default_base_url

    base_url_warning = None
    suggested_base_url = None
    if base_url:
        root, included = _split_base_url(base_url)
        if included:
            suggested_base_url = root
            setting = spec.base_url_env if spec and spec.base_url_env else "base_url"
            base_url_warning = (
                f"{setting} must be the API root without {included}; "
                f"use {root} — the endpoint path is appended automatically"
            )

    extra_headers: list[str] = []
    extra_headers_error = None
    if provider == "openai-compatible" and settings.openai_compatible_extra_headers:
        try:
            extra_headers = sorted(parse_extra_headers(settings.openai_compatible_extra_headers))
        except ValueError as exc:
            extra_headers_error = str(exc)

    if "OPENWIKI_PROVIDER" in os.environ:
        provider_source = "environment"
    elif raw_provider:
        provider_source = "config"
    else:
        provider_source = "default"

    return {
        "provider": provider,
        "provider_source": provider_source,
        "model": model,
        "model_source": source_of("OPENWIKI_MODEL_ID", "default" if model else None),
        "credential_env": credential_env,
        "credential_present": bool(credential_env and env.get(credential_env)),
        "credential_source": source_of(credential_env) if credential_env and env.get(credential_env) else None,
        "base_url": redact_url(base_url) if base_url else None,
        "base_url_source": source_of(spec.base_url_env if spec else None, "default" if base_url else None),
        "base_url_warning": base_url_warning,
        "suggested_base_url": suggested_base_url,
        "extra_headers": extra_headers,
        "extra_headers_error": extra_headers_error,
        "probe_supported": bool(spec and spec.probe),
        "note": spec.note if spec else f"unknown provider {provider!r}",
        "active_check_endpoint": "/health/model",
    }


# ---------------------------------------------------------------------------
# Active probe
# ---------------------------------------------------------------------------
def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:200] if text else "no error details"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
        if isinstance(error, str):
            return error[:300]
        message = data.get("message")
        if isinstance(message, str):
            return message[:300]
    return "no error details"


def _response_error(response: httpx.Response, request_url: str) -> ProbeError:
    content_type = (response.headers.get("content-type") or "").lower()
    body = response.text or ""
    if "html" in content_type or body.lstrip().startswith("<"):
        detail = "HTML page instead of JSON"
        hint = (
            " — the URL does not look like an API endpoint; OpenAI-compatible"
            " base URLs usually end in /v1"
        )
    else:
        detail = _error_message(response)
        hint = ""
    return ProbeError(f"POST {redact_url(request_url)} -> HTTP {response.status_code}: {detail}{hint}")


def _ensure_ok(response: httpx.Response, request_url: str) -> None:
    if response.status_code < 400:
        return
    raise _response_error(response, request_url)


async def _post_with_v1_hint(
    client: httpx.AsyncClient,
    base: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    base_url_env: str | None,
) -> tuple[httpx.Response, str]:
    """POST ``{base}{path}``; on 404/405 probe ``{base}/v1{path}`` as a diagnostic.

    The OpenAI SDKs (and therefore OpenWiki) build request URLs as
    ``baseURL + path``, so a base URL without ``/v1`` fails exactly like this.
    """
    url = f"{base}{path}"
    response = await client.post(url, headers=headers, json=payload)
    if response.status_code in (404, 405) and base and not base.endswith("/v1"):
        alt_url = f"{base}/v1{path}"
        alt: httpx.Response | None
        try:
            alt = await client.post(alt_url, headers=headers, json=payload)
        except httpx.HTTPError:
            alt = None
        if alt is not None and alt.status_code < 400:
            setting = base_url_env or "the base URL setting"
            raise ProbeError(
                f"POST {redact_url(url)} -> HTTP {response.status_code},"
                f" but {redact_url(alt_url)} works; set {setting}={base}/v1"
            )
    _ensure_ok(response, url)
    return response, url


def _openai_responses_text(data: Any) -> str:
    parts: list[str] = []
    output = data.get("output", []) if isinstance(data, dict) else []
    for item in output or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts).strip()


async def _probe_once(
    spec: ProviderSpec,
    provider: str,
    env: dict[str, str],
    model: str,
    timeout: float,
    *,
    base_override: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(answer_text, endpoint)``; raise ProbeError on any failure."""
    credential = (env.get(spec.credential_env) if spec.credential_env else "") or ""
    base = (
        base_override
        or (env.get(spec.base_url_env) if spec.base_url_env else None)
        or spec.default_base_url
        or ""
    ).rstrip("/")
    extras = dict(extra_headers or {})

    def _headers(auth: dict[str, str]) -> dict[str, str]:
        return {"Content-Type": "application/json", **auth, **extras}

    async with _make_client(timeout) as client:
        if spec.probe == "openai-responses":
            response, url = await _post_with_v1_hint(
                client,
                base,
                "/responses",
                headers=_headers({"Authorization": f"Bearer {credential}"}),
                payload={"model": model, "input": PROBE_PROMPT, "max_output_tokens": 1024},
                base_url_env=spec.base_url_env,
            )
            return _openai_responses_text(response.json()), url

        if spec.probe == "anthropic":
            url = f"{base}/v1/messages"
            response = await client.post(
                url,
                headers=_headers({"x-api-key": credential, "anthropic-version": "2023-06-01"}),
                json={"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": PROBE_PROMPT}]},
            )
            _ensure_ok(response, url)
            data = response.json()
            answer = "".join(
                part.get("text", "") for part in (data.get("content") or []) if isinstance(part, dict)
            ).strip()
            return answer, url

        if spec.probe == "gemini":
            model_path = model.removeprefix("models/")
            url = f"{base}/v1beta/models/{model_path}:generateContent"
            response = await client.post(
                url,
                params={"key": credential},
                headers=_headers({}),
                json={
                    "contents": [{"parts": [{"text": PROBE_PROMPT}]}],
                    "generationConfig": {"maxOutputTokens": 128},
                },
            )
            _ensure_ok(response, url)
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return "", url
            parts = (candidates[0].get("content") or {}).get("parts") or []
            answer = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
            return answer, url

        if spec.probe == "chat":
            response, url = await _post_with_v1_hint(
                client,
                base,
                "/chat/completions",
                headers=_headers({"Authorization": f"Bearer {credential}"}),
                payload={"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": PROBE_PROMPT}]},
                base_url_env=spec.base_url_env,
            )
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                return "", url
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # some gateways return content parts
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            answer = content or choices[0].get("text") or ""
            return str(answer).strip(), url

    raise ProbeError(f"active check is not implemented for provider {provider!r}")


class ModelStatusCache:
    """Runs the active probe at most once per TTL window, per app instance."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def check(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._fresh()
        if cached is not None and not force:
            return {**cached, "cached": True}
        async with self._lock:
            cached = self._fresh()
            if cached is not None and not force:
                return {**cached, "cached": True}
            result = await self._run()
            self._cached = result
            self._cached_at = time.monotonic()
            return {**result, "cached": False}

    def _fresh(self) -> dict[str, Any] | None:
        if self._cached is None:
            return None
        ttl = max(self.settings.model_check_ttl_seconds, 0)
        if (time.monotonic() - self._cached_at) >= ttl:
            return None
        return self._cached

    async def _run(self) -> dict[str, Any]:
        info = describe_model(self.settings)
        result: dict[str, Any] = {
            "status": "error",
            "provider": info["provider"],
            "model": info["model"],
            "credential_env": info["credential_env"],
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latency_ms": None,
            "endpoint": None,
            "cached": False,
            "detail": None,
        }

        provider = info["provider"]
        spec = PROVIDERS.get(provider)
        if spec is None:
            result.update(status="unsupported", detail=f"unknown provider {provider!r}; cannot verify the model")
            return result
        if not spec.probe:
            result.update(status="unsupported", detail=spec.note or f"active check not supported for {provider!r}")
            return result

        env = load_openwiki_env(self.settings)
        credential = (env.get(spec.credential_env) if spec.credential_env else None) or None
        model_value = str(info["model"]) if info["model"] else None
        timeout = float(self.settings.model_check_timeout_seconds)

        extra_headers: dict[str, str] = {}
        if provider == "openai-compatible":
            try:
                extra_headers = parse_extra_headers(self.settings.openai_compatible_extra_headers)
            except ValueError as exc:
                result.update(status="error", detail=str(exc))
                return result

        if info.get("base_url_warning"):
            warning = str(info["base_url_warning"])
            suggested = info.get("suggested_base_url")
            if suggested and model_value and (not spec.credential_env or credential):
                # Confirm the suggested base actually works before reporting it.
                started = time.monotonic()
                try:
                    answer, _endpoint = await _probe_once(
                        spec,
                        provider,
                        env,
                        model_value,
                        timeout,
                        base_override=str(suggested),
                        extra_headers=extra_headers,
                    )
                except (ProbeError, httpx.HTTPError):
                    answer = ""
                if answer:
                    result.update(
                        status="error",
                        detail=f"{warning} — with that base the model answers correctly",
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                    return result
            result.update(status="error", detail=warning)
            return result

        if spec.credential_env and not credential:
            result.update(status="not_configured", detail=f"{spec.credential_env} is not set")
            return result
        if not model_value:
            result.update(
                status="not_configured",
                detail="OPENWIKI_MODEL_ID is not set and this provider has no known default model",
            )
            return result
        if spec.base_url_env and not ((env.get(spec.base_url_env) or spec.default_base_url)):
            result.update(status="not_configured", detail=f"{spec.base_url_env} is not set")
            return result

        started = time.monotonic()
        try:
            answer, endpoint = await _probe_once(
                spec, provider, env, model_value, timeout, extra_headers=extra_headers
            )
        except ProbeError as exc:
            result.update(
                status="error",
                detail=redact_text(str(exc), (credential or "",)),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return result
        except httpx.TimeoutException:
            result.update(
                status="error",
                detail=f"model did not answer within {self.settings.model_check_timeout_seconds}s",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return result
        except httpx.HTTPError as exc:
            result.update(
                status="error",
                detail=redact_text(f"HTTP error: {exc}", (credential or "",)),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return result

        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["endpoint"] = redact_url(endpoint)
        if answer:
            result.update(status="ok", detail="provider answered successfully")
        else:
            result.update(
                status="error",
                detail="provider returned an empty completion (streaming-only gateway?)",
            )
        return result
