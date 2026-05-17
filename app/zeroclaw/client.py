"""Synchronous WebSocket client for ZeroClaw /ws/chat."""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from typing import Literal

import websockets

logger = logging.getLogger(__name__)

ApprovalDecision = Literal["approve", "deny", "always", "timeout"]


async def _chat_stream_async(
    ws_url: str,
    text: str,
    on_approval_request: Callable[[dict], ApprovalDecision] | None = None,
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
                logger.debug("ws_rx type=%s frame=%s", ftype, frame)
            if ftype == "approval_request" and on_approval_request is not None:
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                logger.info(
                    "approval_request received request_id=%s timeout_secs=%s tool=%s",
                    frame.get("request_id"),
                    frame.get("timeout_secs"),
                    frame.get("tool"),
                )
                decision = await loop.run_in_executor(None, on_approval_request, frame)
                logger.info(
                    "approval_response sending decision=%s elapsed=%.1fs request_id=%s",
                    decision,
                    loop.time() - t0,
                    frame.get("request_id"),
                )
                response = {
                    "type": "approval_response",
                    "request_id": frame.get("request_id"),
                    "decision": decision,
                }
                await ws.send(json.dumps(response))
            yield frame
            if ftype in ("done", "error"):
                break


def chat_stream(
    ws_url: str,
    text: str,
    on_approval_request: Callable[[dict], ApprovalDecision] | None = None,
) -> Iterator[dict]:
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
