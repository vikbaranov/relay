"""Tiny HTTP server exposing /healthz and /readyz for the controller pod's own K8s probes."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_ready = threading.Event()


def mark_ready() -> None:
    _ready.set()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, b"ok")
        elif self.path == "/readyz":
            if _ready.is_set():
                self._respond(200, b"ok")
            else:
                self._respond(503, b"not ready")
        else:
            self._respond(404, b"not found")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # silence default access log
        pass


def start(port: int = 8080) -> None:
    server = HTTPServer(("", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
