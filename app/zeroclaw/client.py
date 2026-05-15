"""Synchronous WebSocket client for ZeroClaw /ws/chat."""

import asyncio
import json
import logging
from collections.abc import Iterator

import websockets

logger = logging.getLogger(__name__)


async def _chat_async(ws_url: str, text: str) -> str:
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"type": "message", "content": text}))
        chunks: list[str] = []
        async for raw in ws:
            frame = json.loads(raw)
            ftype = frame.get("type")
            if ftype == "chunk":
                chunks.append(frame.get("content", ""))
            elif ftype == "done":
                return frame.get("full_response") or "".join(chunks)
            elif ftype == "error":
                raise RuntimeError(f"ZeroClaw error: {frame.get('message')}")
        return "".join(chunks)


async def _chat_stream_async(ws_url: str, text: str):
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"type": "message", "content": text}))
        async for raw in ws:
            frame = json.loads(raw)
            yield frame
            if frame.get("type") in ("done", "error"):
                break


def chat(ws_url: str, text: str) -> str:
    """Send a message to ZeroClaw and return the complete response."""
    return asyncio.run(_chat_async(ws_url, text))


def chat_stream(ws_url: str, text: str) -> Iterator[dict]:
    """Yield WebSocket frames from ZeroClaw as they arrive."""
    loop = asyncio.new_event_loop()
    agen = _chat_stream_async(ws_url, text)
    try:
        while True:
            try:
                frame = loop.run_until_complete(agen.__anext__())
                yield frame
                if frame.get("type") in ("done", "error"):
                    break
            except StopAsyncIteration:
                break
    finally:
        loop.run_until_complete(agen.aclose())
        loop.close()
