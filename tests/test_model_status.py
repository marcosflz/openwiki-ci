"""Tests for the model configuration status and active model probe."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services import model_status

ENV_KEYS = (
    "OPENWIKI_PROVIDER",
    "OPENWIKI_MODEL_ID",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
)


def clear_model_env(monkeypatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def mock_transport(monkeypatch, handler):
    """Route the probe's httpx client through an in-process mock transport."""
    calls = {"count": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return handler(request)

    transport = httpx.MockTransport(_handler)

    def _client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    monkeypatch.setattr(model_status, "_make_client", _client)
    return calls


def responses_ok(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]},
    )


def write_config_env(settings, **values: str) -> None:
    settings.resolved_config_dir.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    (settings.resolved_config_dir / ".env").write_text(content, encoding="utf-8")


# --- passive configuration ---------------------------------------------------


def test_health_reports_default_provider_and_model(client, monkeypatch):
    clear_model_env(monkeypatch)
    payload = client.get("/health").json()["model"]
    assert payload["provider"] == "openai"
    assert payload["provider_source"] == "default"
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["model_source"] == "default"
    assert payload["credential_env"] == "OPENAI_API_KEY"
    assert payload["credential_present"] is False
    assert payload["probe_supported"] is True


def test_health_reads_provider_from_config_dir_env(client, settings, monkeypatch):
    clear_model_env(monkeypatch)
    write_config_env(settings, OPENWIKI_PROVIDER="anthropic", ANTHROPIC_API_KEY="test-key")
    payload = client.get("/health").json()["model"]
    assert payload["provider"] == "anthropic"
    assert payload["provider_source"] == "config"
    assert payload["credential_present"] is True
    assert payload["credential_source"] == "config"
    assert payload["model"] is None


def test_environment_overrides_config_dir_env(client, settings, monkeypatch):
    clear_model_env(monkeypatch)
    write_config_env(settings, OPENWIKI_PROVIDER="anthropic", ANTHROPIC_API_KEY="from-file")
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    payload = client.get("/health").json()["model"]
    assert payload["provider"] == "openrouter"
    assert payload["provider_source"] == "environment"
    assert payload["credential_env"] == "OPENROUTER_API_KEY"
    assert payload["credential_present"] is True


# --- active probe ------------------------------------------------------------


def test_model_check_ok_and_cache(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = mock_transport(monkeypatch, lambda request: responses_ok("ok"))

    first = client.get("/health/model")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "ok"
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-5.6-terra"
    assert body["cached"] is False
    assert body["endpoint"] == "https://api.openai.com/v1/responses"
    assert isinstance(body["latency_ms"], int)

    second = client.get("/health/model")
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert calls["count"] == 1

    forced = client.get("/health/model", params={"force": "true"})
    assert forced.status_code == 200
    assert forced.json()["cached"] is False
    assert calls["count"] == 2


def test_model_check_provider_error_is_redacted(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            401, json={"error": {"message": "Incorrect API key provided: super-secret-key"}}
        ),
    )
    response = client.get("/health/model")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert "HTTP 401" in body["detail"]
    assert "super-secret-key" not in body["detail"]
    assert "***" in body["detail"]


def test_model_check_flags_empty_completion(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mock_transport(monkeypatch, lambda request: httpx.Response(200, json={"output": []}))
    response = client.get("/health/model")
    assert response.status_code == 503
    assert "empty completion" in response.json()["detail"]


def test_model_check_not_configured_without_key(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai")
    response = client.get("/health/model")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_configured"
    assert "OPENAI_API_KEY" in body["detail"]


def test_model_check_openai_compatible_requires_base_url(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "ollama")
    monkeypatch.setenv("OPENWIKI_MODEL_ID", "llama3.2")
    response = client.get("/health/model")
    assert response.status_code == 503
    assert "OPENAI_COMPATIBLE_BASE_URL" in response.json()["detail"]


def test_model_check_unsupported_and_unknown_providers(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "bedrock")
    response = client.get("/health/model")
    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"
    assert "IAM" in response.json()["detail"]

    monkeypatch.setenv("OPENWIKI_PROVIDER", "acme")
    response = client.get("/health/model", params={"force": "true"})
    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"
    assert "unknown provider" in response.json()["detail"]


def test_model_check_anthropic_uses_messages_api(client, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENWIKI_MODEL_ID", "claude-sonnet-5")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    mock_transport(monkeypatch, handler)
    response = client.get("/health/model")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert seen["path"] == "/v1/messages"
    assert seen["key"] == "test-key"


def _configure_openai_compatible(client, monkeypatch, base: str = "https://gateway.example.com") -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", base)
    monkeypatch.setenv("OPENWIKI_MODEL_ID", "deepseek-v4-flash")


def test_model_check_diagnoses_html_404(client, monkeypatch):
    _configure_openai_compatible(client, monkeypatch)
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            404, headers={"content-type": "text/html"}, text="<!DOCTYPE html><html>nope</html>"
        ),
    )
    response = client.get("/health/model")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "HTTP 404" in detail
    assert "HTML page" in detail
    assert "https://gateway.example.com/chat/completions" in detail


def test_model_check_hints_missing_v1_in_base_url(client, monkeypatch):
    _configure_openai_compatible(client, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(404, headers={"content-type": "text/html"}, text="<html></html>")

    mock_transport(monkeypatch, handler)
    response = client.get("/health/model")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "OPENAI_COMPATIBLE_BASE_URL=https://gateway.example.com/v1" in detail


def test_health_reports_base_url_warning(client, monkeypatch):
    _configure_openai_compatible(client, monkeypatch, base="https://gateway.example.com/v1/chat/completions")
    model = client.get("/health").json()["model"]
    assert "must be the API root" in model["base_url_warning"]
    assert model["suggested_base_url"] == "https://gateway.example.com/v1"


def test_model_check_flags_endpoint_path_and_confirms_fix(client, monkeypatch):
    _configure_openai_compatible(
        client, monkeypatch, base="https://opencode.ai/zen/go/v1/chat/completions"
    )
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/zen/go/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(404, headers={"content-type": "text/html"}, text="<html></html>")

    mock_transport(monkeypatch, handler)
    response = client.get("/health/model")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert "must be the API root" in body["detail"]
    assert "answers correctly" in body["detail"]
    assert "https://opencode.ai/zen/go/v1" in body["detail"]
    # only the corrected base was probed, never the doubled path
    assert seen_paths == ["/zen/go/v1/chat/completions"]


def test_model_check_flags_endpoint_path_without_confirmation(client, monkeypatch):
    _configure_openai_compatible(client, monkeypatch, base="https://gateway.example.com/chat/completions")
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": {"message": "bad key"}}),
    )
    response = client.get("/health/model")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "must be the API root" in detail
    assert "answers correctly" not in detail


def test_probe_sends_extra_headers(client, settings, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://gateway.example.com/v1")
    monkeypatch.setenv("OPENWIKI_MODEL_ID", "deepseek-v4-flash")
    settings.openai_compatible_extra_headers = '{"x-opencode-session": "sess-1"}'
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    mock_transport(monkeypatch, handler)
    response = client.get("/health/model", params={"force": "true"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["x-opencode-session"] == "sess-1"
    assert captured["user-agent"].startswith("openwiki-service/")


def test_health_lists_extra_header_names_without_values(client, settings, monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENWIKI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://gateway.example.com/v1")
    settings.openai_compatible_extra_headers = '{"x-opencode-session": "top-secret-value"}'
    model = client.get("/health").json()["model"]
    assert "x-opencode-session" in model["extra_headers"]
    assert "top-secret-value" not in json.dumps(model)
