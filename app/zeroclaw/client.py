"""Synchronous WebSocket client for ZeroClaw /ws/chat."""

import asyncio
import json
import logging

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


def chat(ws_url: str, text: str) -> str:
    """Send a message to ZeroClaw and return the complete response."""
    return asyncio.run(_chat_async(ws_url, text))
