"""Tiny HTTP server exposing /healthz and /readyz for the controller pod's own K8s probes."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

_ready = threading.Event()


def mark_ready() -> None:
    _ready.set()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, {"status": "ok"})
        elif self.path == "/readyz":
            ready = _ready.is_set()
            self._respond(200 if ready else 503, {"ready": ready})
        elif self.path == "/metrics":
            self._respond_raw(200, generate_latest(), CONTENT_TYPE_LATEST)
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict) -> None:
        self._respond_raw(code, json.dumps(body).encode(), "application/json")

    def _respond_raw(self, code: int, data: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):  # silence default access log
        pass


def start(port: int = 8080) -> None:
    server = HTTPServer(("", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
