import logging
import threading
import time
from collections.abc import Callable

from app.bot.formatting import (
    _CURSOR,
    _HEARTBEAT_INTERVAL,
    _UPDATE_INTERVAL,
    _fmt_tool_done,
    _fmt_tool_running,
    _key_arg,
)

logger = logging.getLogger(__name__)


class StreamHandler:
    def __init__(self, patch_fn: Callable[[str], None], extra: dict) -> None:
        self._patch = patch_fn
        self._extra = extra
        self._chunks: list[str] = []
        self._tool_log: list[str] = []
        self._current_tool: str = ""
        self._current_tool_key: str = ""
        self._last_update = time.monotonic()
        self._stream_start = time.monotonic()

    def render(self, cursor: bool = False) -> str:
        parts = []
        all_tools = self._tool_log + ([self._current_tool] if self._current_tool else [])
        if all_tools:
            parts.append("\n".join(all_tools))
        text = "".join(self._chunks)
        if text:
            parts.append(text + (_CURSOR if cursor else ""))
        return "\n\n".join(parts) if parts else ""

    def heartbeat(self, stop: threading.Event) -> None:
        tick = 0
        while not stop.wait(timeout=_HEARTBEAT_INTERVAL):
            if self._tool_log or self._current_tool or self._chunks:
                continue
            elapsed = int(time.monotonic() - self._stream_start)
            dots = "." * (tick % 3 + 1)
            self._patch(f"_Думаю{dots}_ ({elapsed}с)")
            tick += 1

    def handle_frame(self, frame: dict) -> bool:
        ftype = frame.get("type")
        if ftype == "chunk":
            self._chunks.append(frame.get("content", ""))
            self._current_tool = ""
            now = time.monotonic()
            if now - self._last_update >= _UPDATE_INTERVAL:
                self._patch(self.render(cursor=True))
                self._last_update = now
        elif ftype == "tool_call":
            tool_name = frame.get("name", "?")
            args = frame.get("args")
            self._current_tool_key = _key_arg(tool_name, args)
            self._current_tool = _fmt_tool_running(tool_name, self._current_tool_key)
            logger.debug(
                "tool_call tool=%s args=%s call_id=%s",
                tool_name,
                args,
                frame.get("id"),
                extra=self._extra,
            )
            self._patch(self.render())
            self._last_update = time.monotonic()
        elif ftype == "tool_result":
            tool_name = frame.get("name", "?")
            output = frame.get("output", "")
            logger.debug(
                "tool_result tool=%s output=%s call_id=%s",
                tool_name,
                output[:200],
                frame.get("id"),
                extra=self._extra,
            )
            self._tool_log.append(_fmt_tool_done(tool_name, self._current_tool_key, output))
            self._current_tool = ""
            self._current_tool_key = ""
            self._patch(self.render())
            self._last_update = time.monotonic()
        elif ftype == "done":
            final = frame.get("full_response") or "".join(self._chunks)
            tool_section = "\n".join(self._tool_log) if self._tool_log else ""
            if tool_section and final:
                self._patch(f"{tool_section}\n\n{final}")
            elif tool_section:
                self._patch(tool_section)
            else:
                self._patch(final)
            return True
        elif ftype == "error":
            raise RuntimeError(f"ZeroClaw error: {frame.get('message')}")
        elif ftype in ("session_start", "approval_request"):
            pass
        elif ftype is not None:
            logger.debug("unhandled zeroclaw frame type=%s frame=%s", ftype, frame)
        return False

    def handle_stream_end(self, rkey: str) -> None:
        final = self.render()
        if final:
            self._patch(final)
        else:
            logger.warning(
                "zeroclaw stream ended without 'done' frame for %s", rkey, extra=self._extra
            )
            self._patch("Соединение с агентом прервано. Попробуйте ещё раз.")
