import logging
import threading
import time
from collections.abc import Callable

from app import metrics
from app.bot.formatting import (
    _CURSOR,
    _HEARTBEAT_INTERVAL,
    _THINKING_BUFFER_MAX,
    _THINKING_PREVIEW_MAX,
    _UPDATE_INTERVAL,
    _fmt_tool_done,
    _fmt_tool_running,
    _key_arg,
    _tail,
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
        t = time.monotonic()
        self._last_update = t
        self._stream_start = t
        self._pending_tool_start: dict[str, tuple[str, float]] = {}
        self._thinking = ""

    def _patch_if_due(self, *, cursor: bool = False) -> None:
        now = time.monotonic()
        if now - self._last_update >= _UPDATE_INTERVAL:
            self._patch(self.render(cursor=cursor))
            self._last_update = now

    def _clear_stream_chunks(self) -> None:
        self._chunks.clear()
        self._current_tool = ""

    def render(self, cursor: bool = False) -> str:
        parts = []
        all_tools = self._tool_log + ([self._current_tool] if self._current_tool else [])
        if all_tools:
            parts.append("\n".join(all_tools))
        text = "".join(self._chunks)
        if text:
            parts.append(text + (_CURSOR if cursor else ""))
        elif self._thinking and not all_tools:
            cursor_or_empty = _CURSOR if cursor else ""
            parts.append(f"_💭 {_tail(self._thinking, _THINKING_PREVIEW_MAX)}{cursor_or_empty}_")
        return "\n\n".join(parts) if parts else ""

    def heartbeat(self, stop: threading.Event) -> None:
        tick = 0
        while not stop.wait(timeout=_HEARTBEAT_INTERVAL):
            if self._tool_log or self._current_tool or self._chunks or self._thinking:
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
            self._patch_if_due(cursor=True)
        elif ftype == "tool_call":
            tool_name = frame.get("name", "?")
            call_id = frame.get("id") or tool_name
            args = frame.get("args")
            self._current_tool_key = _key_arg(tool_name, args)
            self._current_tool = _fmt_tool_running(tool_name, self._current_tool_key)
            metrics.tool_calls_total.labels(tool=tool_name).inc()
            self._pending_tool_start[call_id] = (tool_name, time.monotonic())
            logger.debug(
                "tool_call tool=%s args=%s call_id=%s",
                tool_name,
                args,
                call_id,
                extra=self._extra,
            )
            self._patch(self.render())
            self._last_update = time.monotonic()
        elif ftype == "tool_result":
            tool_name = frame.get("name", "?")
            call_id = frame.get("id") or tool_name
            output = frame.get("output", "")
            pending = self._pending_tool_start.pop(call_id, None)
            if pending is not None:
                metrics.tool_call_duration_seconds.labels(tool=pending[0]).observe(
                    time.monotonic() - pending[1]
                )
            logger.debug(
                "tool_result tool=%s output=%s call_id=%s",
                tool_name,
                output[:200],
                call_id,
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
            self._patch(final if final else tool_section)
            model = frame.get("model") or "unknown"
            input_tokens = frame.get("input_tokens") or 0
            output_tokens = frame.get("output_tokens") or 0
            metrics.llm_request_duration_seconds.observe(time.monotonic() - self._stream_start)
            if input_tokens or output_tokens:
                metrics.tokens_total.labels(kind="input", model=model).inc(input_tokens)
                metrics.tokens_total.labels(kind="output", model=model).inc(output_tokens)
            logger.info(
                "llm_usage model=%s provider=%s input_tokens=%d output_tokens=%d",
                model,
                frame.get("provider", "unknown"),
                input_tokens,
                output_tokens,
                extra=self._extra,
            )
            return True
        elif ftype == "error":
            raise RuntimeError(f"ZeroClaw error: {frame.get('message')}")
        elif ftype == "thinking":
            self._thinking += frame.get("content", "")
            if len(self._thinking) > _THINKING_BUFFER_MAX:
                self._thinking = self._thinking[-_THINKING_BUFFER_MAX:]
            self._patch_if_due(cursor=True)
        elif ftype == "chunk_reset":
            self._clear_stream_chunks()
        elif ftype in ("session_start", "approval_request"):
            pass
        elif ftype is not None:
            logger.debug("unhandled zeroclaw frame type=%s frame=%s", ftype, frame)
        return False

    def handle_stream_end(self, rkey: str) -> None:
        self._pending_tool_start.clear()
        final = self.render()
        if final:
            self._patch(final)
        else:
            logger.warning(
                "zeroclaw stream ended without 'done' frame for %s", rkey, extra=self._extra
            )
            self._patch("Соединение с агентом прервано. Попробуйте ещё раз.")
