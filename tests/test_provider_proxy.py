"""Tests for the header-injecting proxy used with OpenAI-compatible gateways."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from app.services.provider_proxy import create_forward_app, parse_extra_headers
from tests.conftest import FAKE_OPENWIKI, wait_for_status


def test_parse_extra_headers_defaults_user_agent():
    headers = parse_extra_headers('{"x-opencode-session": "abc"}')
    assert headers["x-opencode-session"] == "abc"
    assert headers["User-Agent"].startswith("openwiki-service/")


def test_parse_extra_headers_keeps_configured_user_agent():
    assert parse_extra_headers('{"User-Agent": "my-agent/1.0"}') == {"User-Agent": "my-agent/1.0"}


def test_parse_extra_headers_empty():
    assert parse_extra_headers(None) == {}
    assert parse_extra_headers("   ") == {}


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '{"a": {"b": 1}}', '{"a": null}'])
def test_parse_extra_headers_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_extra_headers(raw)


def test_create_app_rejects_invalid_extra_headers(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", openai_compatible_extra_headers="{not json}")
    with pytest.raises(ValueError):
        create_app(settings)


class _JsonStream(httpx.AsyncByteStream):
    """Minimal async stream so the mock response supports ``aiter_raw``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):
        yield self._payload


def test_forward_app_injects_headers_and_preserves_path():
    async def scenario():
        captured: dict = {}

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_JsonStream(b'{"ok": true}'),
            )

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
        app = create_forward_app(
            "https://opencode.ai/zen/go/v1",
            {"x-opencode-session": "sess", "User-Agent": "openwiki-service/test"},
            client=upstream,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy"
        ) as proxy_client:
            response = await proxy_client.post(
                "/zen/go/v1/chat/completions",
                json={"model": "deepseek-v4-flash"},
                headers={"Authorization": "Bearer key"},
            )
        await upstream.aclose()
        return response, captured

    response, captured = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["headers"]["x-opencode-session"] == "sess"
    assert captured["headers"]["user-agent"] == "openwiki-service/test"
    assert captured["headers"]["authorization"] == "Bearer key"


def test_forward_app_rejects_paths_outside_base():
    async def scenario():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        app = create_forward_app("https://host/zen/go/v1", {}, client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy"
        ) as proxy_client:
            response = await proxy_client.get("/other/path")
        await upstream.aclose()
        return response

    assert asyncio.run(scenario()).status_code == 404


def test_wikis_point_openwiki_at_the_proxy(tmp_path, make_git_repo):
    from fastapi.testclient import TestClient

    settings = Settings(
        data_dir=tmp_path / "data",
        openwiki_bin=f'"{sys.executable}" "{FAKE_OPENWIKI}"',
        allow_local_git=True,
        run_local_worker=True,
        max_concurrent_jobs=1,
        worker_poll_seconds=0.1,
        compat_proxy_port=0,
        openai_compatible_base_url="https://upstream.example.com/zen/go/v1",
        openai_compatible_extra_headers='{"x-opencode-session": "sess"}',
    )
    repo = make_git_repo("proxy-repo")

    with TestClient(create_app(settings)) as client:
        proxy = client.app.state.proxy
        assert proxy is not None
        assert proxy.base_url.startswith("http://127.0.0.1:")
        assert proxy.base_url.endswith("/zen/go/v1")

        response = client.post("/wikis", json={"source": {"url": str(repo)}})
        payload = wait_for_status(client, response.json()["wiki_id"])
        assert payload["status"] == "done", payload

        download = client.get(f"/wikis/{payload['wiki_id']}/download")
        with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
            env = json.loads(zf.read(".openwiki/.fake-env.json"))
        # OpenWiki received the loopback proxy URL, not the upstream one.
        assert env["OPENAI_COMPATIBLE_BASE_URL"] == proxy.base_url
