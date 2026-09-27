"""Loopback forwarding proxy that injects custom headers into provider calls.

Some OpenAI-compatible gateways require identifying request headers that the
OpenWiki CLI cannot send by itself (for example OpenCode Go requires
``x-opencode-session`` and a client user agent). When
``OPENAI_COMPATIBLE_EXTRA_HEADERS`` is configured, the service starts a
loopback proxy and points OpenWiki's ``OPENAI_COMPATIBLE_BASE_URL`` at it; the
proxy forwards every request to the real base URL adding those headers.

The health probe talks to the upstream directly with the same headers, so
``/health/model`` keeps testing the real endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Mapping

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .. import __version__

DEFAULT_USER_AGENT = f"openwiki-service/{__version__}"

#: Headers owned by the transport; never forwarded in either direction.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

UPSTREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=None, pool=None)


def parse_extra_headers(raw: str | None) -> dict[str, str]:
    """Parse ``OPENAI_COMPATIBLE_EXTRA_HEADERS`` (JSON object of string values).

    A default ``User-Agent`` identifying this service is added unless the user
    configured one, because generic SDK user agents are rejected by some
    gateways.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "OPENAI_COMPATIBLE_EXTRA_HEADERS must be a JSON object such as "
            '{"x-opencode-session": "openwiki-service"}; '
            f"got invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("OPENAI_COMPATIBLE_EXTRA_HEADERS must be a JSON object")
    headers: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or isinstance(value, (dict, list)) or value is None:
            raise ValueError("OPENAI_COMPATIBLE_EXTRA_HEADERS keys and values must be strings")
        headers[key] = str(value)
    if not any(key.lower() == "user-agent" for key in headers):
        headers["User-Agent"] = DEFAULT_USER_AGENT
    return headers


def _replace_headers(headers: dict[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """Case-insensitive replacement so extra headers never duplicate keys."""
    merged = dict(headers)
    for key, value in extra.items():
        for existing in [name for name in merged if name.lower() == key.lower()]:
            merged.pop(existing)
        merged[key] = value
    return merged


def create_forward_app(
    upstream_base: str,
    extra_headers: Mapping[str, str],
    *,
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Starlette app that forwards everything to ``upstream_base`` + headers."""
    upstream = httpx.URL(upstream_base)
    base_path = upstream.path.rstrip("/")
    own_client: httpx.AsyncClient | None = None
    if client is None:
        own_client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
        client = own_client

    async def forward(request: Request) -> Response:
        path = request.url.path
        if base_path and not path.startswith(base_path):
            return Response(
                "openwiki-service proxy: path outside the configured base URL",
                status_code=404,
            )

        target = httpx.URL(
            scheme=upstream.scheme,
            host=upstream.host,
            port=upstream.port,
            path=path,
            query=request.url.query.encode("utf-8") or None,
        )
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}
        headers = _replace_headers(headers, extra_headers)
        upstream_request = client.build_request(
            request.method,
            target,
            headers=headers,
            content=await request.body(),
        )
        upstream_response = await client.send(upstream_request, stream=True)
        response_headers = {
            key: value for key, value in upstream_response.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS
        }
        return StreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream_response.aclose),
        )

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                forward,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ]
    )
    if own_client is not None:
        app.state.http_client = own_client
    return app


class _ProxyServer(uvicorn.Server):
    """Uvicorn server that does not touch process signal handlers."""

    def install_signal_handlers(self) -> None:  # pragma: no cover - trivial
        pass


@dataclass
class ProxyHandle:
    base_url: str
    port: int
    _server: _ProxyServer
    _task: asyncio.Task[None]
    _client: httpx.AsyncClient | None

    async def stop(self) -> None:
        self._server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, timeout=10)
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()


async def start_proxy(
    upstream_base: str,
    extra_headers: Mapping[str, str],
    *,
    port: int,
) -> ProxyHandle:
    """Start the loopback proxy and return its handle (``port=0`` picks a free port)."""
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
    app = create_forward_app(upstream_base, extra_headers, client=client)
    server = _ProxyServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    task = asyncio.create_task(server.serve(), name="openwiki-compat-proxy")
    for _ in range(200):
        if server.started:
            break
        if task.done():
            with contextlib.suppress(Exception):
                task.result()
            raise RuntimeError("the OpenAI-compatible proxy failed to start")
        await asyncio.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("timed out starting the OpenAI-compatible proxy")

    bound_port = server.servers[0].sockets[0].getsockname()[1]
    prefix = httpx.URL(upstream_base).path.rstrip("/")
    return ProxyHandle(
        base_url=f"http://127.0.0.1:{bound_port}{prefix}",
        port=bound_port,
        _server=server,
        _task=task,
        _client=client,
    )
