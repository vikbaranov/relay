"""Synchronous WebSocket client for ZeroClaw /ws/chat."""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from typing import Optional

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


async def _chat_stream_async(
    ws_url: str,
    text: str,
    on_approval_request: Optional[Callable[[dict], bool]] = None,
):
    async with websockets.connect(
        ws_url,
        ping_interval=20,
        ping_timeout=20,
        open_timeout=30,
        close_timeout=10,
    ) as ws:
        await ws.send(json.dumps({"type": "message", "content": text}))
        async for raw in ws:
            frame = json.loads(raw)
            ftype = frame.get("type")
            if ftype not in ("chunk",):
                logger.info("ws_rx type=%s frame=%s", ftype, frame)
            if ftype == "approval_request" and on_approval_request is not None:
                t0 = asyncio.get_event_loop().time()
                logger.info(
                    "approval_request received request_id=%s timeout_secs=%s tool=%s",
                    frame.get("request_id"),
                    frame.get("timeout_secs"),
                    frame.get("tool"),
                )
                loop = asyncio.get_event_loop()
                approved = await loop.run_in_executor(None, on_approval_request, frame)
                elapsed = asyncio.get_event_loop().time() - t0
                logger.info(
                    "approval_response sending approved=%s elapsed=%.1fs request_id=%s",
                    approved,
                    elapsed,
                    frame.get("request_id"),
                )
                await ws.send(json.dumps({
                    "type": "approval_response",
                    "request_id": frame["request_id"],
                    "decision": "approve" if approved else "deny",
                }))
            yield frame
            if ftype in ("done", "error"):
                break


def chat(ws_url: str, text: str) -> str:
    """Send a message to ZeroClaw and return the complete response."""
    return asyncio.run(_chat_async(ws_url, text))


def chat_stream(
    ws_url: str,
    text: str,
    on_approval_request: Optional[Callable[[dict], bool]] = None,
) -> Iterator[dict]:
    """Yield WebSocket frames from ZeroClaw as they arrive."""
    loop = asyncio.new_event_loop()
    agen = _chat_stream_async(ws_url, text, on_approval_request=on_approval_request)
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
