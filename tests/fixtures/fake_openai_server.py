#!/usr/bin/env python
"""Tiny mock of an OpenAI-compatible API for manual/e2e checks of /health/model.

Serves the minimal Responses and chat-completions shapes used by the probe::

    python fake_openai_server.py [port]   # default 9000
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: When 1, the mock behaves like OpenCode Go: 400 without x-opencode-session.
REQUIRE_SESSION = os.environ.get("FAKE_REQUIRE_SESSION") == "1"


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if REQUIRE_SESSION and not self.headers.get("x-opencode-session"):
            self._json(400, {"error": {"message": "Request is missing x-opencode-session"}})
            return
        if self.path.endswith("/responses"):
            body = {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
                ]
            }
        elif self.path.endswith("/chat/completions"):
            body = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        self._json(200, body)

    def log_message(self, *args) -> None:  # keep the container logs quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
