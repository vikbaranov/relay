"""ZeroClaw websocket client tests."""

import json

from app.zeroclaw import client


class _FakeWebSocket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self.frames))
        except StopIteration:
            raise StopAsyncIteration from None


def test_approval_response_preserves_request_id_and_decision(monkeypatch):
    ws = _FakeWebSocket(
        [
            {"type": "approval_request", "request_id": "req-1", "tool": "shell"},
            {"type": "done", "full_response": "ok"},
        ]
    )
    monkeypatch.setattr(client.websockets, "connect", lambda *args, **kwargs: ws)

    list(client.chat_stream("ws://example", "hello", on_approval_request=lambda frame: "always"))

    assert ws.sent[0] == {"type": "message", "content": "hello"}
    assert ws.sent[1] == {
        "type": "approval_response",
        "request_id": "req-1",
        "decision": "always",
    }


def test_approval_response_surfaces_timeout(monkeypatch):
    ws = _FakeWebSocket(
        [
            {"type": "approval_request", "request_id": "req-2", "tool": "shell"},
            {"type": "done", "full_response": "ok"},
        ]
    )
    monkeypatch.setattr(client.websockets, "connect", lambda *args, **kwargs: ws)

    list(client.chat_stream("ws://example", "hello", on_approval_request=lambda frame: "timeout"))

    assert ws.sent[1] == {
        "type": "approval_response",
        "request_id": "req-2",
        "decision": "timeout",
    }
