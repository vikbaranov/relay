import logging
import threading
import time

from mmpy_bot.function import listen_to, listen_webhook
from mmpy_bot.plugins.base import Plugin
from mmpy_bot.wrappers import ActionEvent, Message

from app.config import Settings
from app.identity import object_name, session_id
from app.k8s.runtime import RuntimeManager
from app.zeroclaw.client import chat_stream

logger = logging.getLogger(__name__)

_CURSOR = "▌"
_UPDATE_INTERVAL = 1.0  # seconds between incremental patches
_RESULT_MAX = 150
_HEARTBEAT_INTERVAL = 10.0  # seconds between thinking-indicator updates
_MM_MAX_POST = 2_000

_TOOL_ICONS: dict[str, str] = {
    "web_search": "🔍",
    "web_search_tool": "🔍",
    "web_fetch": "🌐",
    "bash": "💻",
    "execute_bash": "💻",
    "shell": "💻",
    "python": "🐍",
    "execute_python": "🐍",
    "read_file": "📄",
    "write_file": "✏️",
    "list_files": "📁",
}
_TOOL_ICON_DEFAULT = "⚙️"


def _key_arg(name: str, args: dict | None) -> str:
    if not args:
        return ""
    val = next(iter(args.values()), "")
    s = str(val).strip()
    if s.startswith(("http://", "https://")):
        try:
            from urllib.parse import urlparse

            p = urlparse(s)
            path = p.path[:50] if len(p.path) > 50 else p.path
            s = p.netloc + path
        except Exception:
            pass
    if len(s) > 80:
        s = s[:77] + "..."
    return s


def _fmt_tool_running(name: str, args: dict | None) -> str:
    icon = _TOOL_ICONS.get(name, _TOOL_ICON_DEFAULT)
    key = _key_arg(name, args)
    if key:
        return f"_{icon} `{name}`: {key}..._"
    return f"_{icon} `{name}`..._"


def _fmt_tool_done(name: str, key: str, output: str) -> str:
    icon = _TOOL_ICONS.get(name, _TOOL_ICON_DEFAULT)
    out = output.strip()
    if "no results found" in out.lower():
        summary = "нет результатов"
    elif len(out) > _RESULT_MAX:
        summary = out[:_RESULT_MAX] + "..."
    else:
        summary = out
    if key:
        return f"_{icon} `{name}`: {key} → {summary}_"
    return f"_{icon} `{name}` → {summary}_"


class ZeroClawPlugin(Plugin):
    def __init__(self, settings: Settings, runtime: RuntimeManager) -> None:
        super().__init__()
        self._settings = settings
        self._runtime = runtime
        self._secret = settings.k8s_name_secret.encode()
        self._pending_approvals: dict[str, dict] = {}

    def _patch(self, post_id: str, text: str) -> None:
        if len(text) > _MM_MAX_POST:
            text = text[: _MM_MAX_POST - 60] + "\n\n_[ответ обрезан — слишком длинный]_"
        self.driver.posts.patch_post(post_id, {"message": text})

    def _request_approval(
        self, frame: dict, channel_id: str, root_id: str, main_post_id: str
    ) -> bool:
        request_id = frame["request_id"]
        tool = frame["tool"]
        summary = frame.get("arguments_summary", "")
        timeout = frame.get("timeout_secs", 120)

        base = (
            self._settings.webhook_public_url
            or f"http://localhost:{self._settings.webhook_host_port}"
        )
        webhook_url = f"{base}/hooks/approval"

        self._patch(main_post_id, "_Ожидание подтверждения..._")

        approval_post = self.driver.posts.create_post(
            options={
                "channel_id": channel_id,
                "root_id": root_id,
                "props": {
                    "attachments": [
                        {
                            "text": f"**Подтверждение действия**\nИнструмент: `{tool}`\n{summary}",
                            "actions": [
                                {
                                    "id": "approve",
                                    "name": "✅ Разрешить",
                                    "type": "button",
                                    "integration": {
                                        "url": webhook_url,
                                        "context": {
                                            "request_id": request_id,
                                            "approved": True,
                                            "main_post_id": main_post_id,
                                        },
                                    },
                                },
                                {
                                    "id": "deny",
                                    "name": "❌ Отклонить",
                                    "type": "button",
                                    "integration": {
                                        "url": webhook_url,
                                        "context": {
                                            "request_id": request_id,
                                            "approved": False,
                                            "main_post_id": main_post_id,
                                        },
                                    },
                                },
                            ],
                        }
                    ]
                },
            }
        )
        approval_post_id = approval_post["id"]

        event = threading.Event()
        self._pending_approvals[request_id] = {
            "event": event,
            "approved": False,
            "approval_post_id": approval_post_id,
        }

        if event.wait(timeout=timeout):
            approved = self._pending_approvals.pop(request_id, {}).get("approved", False)
        else:
            self._pending_approvals.pop(request_id, None)
            approved = False
            self.driver.posts.patch_post(
                approval_post_id,
                {
                    "props": {
                        "attachments": [{"text": "⏱ Таймаут. Действие отклонено автоматически."}]
                    }
                },
            )

        return approved

    @listen_webhook("approval")
    def handle_approval(self, event: ActionEvent) -> None:
        context = event.context or {}
        request_id = context.get("request_id")
        approved = bool(context.get("approved", False))

        pending = self._pending_approvals.get(request_id)
        if not pending:
            self.driver.respond_to_web(
                event, {"update": {"message": "Запрос не найден или истёк."}}
            )
            return

        pending["approved"] = approved
        pending["event"].set()

        status = "✅ Разрешено" if approved else "❌ Отклонено"
        self.driver.respond_to_web(
            event,
            {
                "update": {
                    "props": {
                        "attachments": [
                            {
                                "text": f"**Подтверждение действия**\n{status} пользователем @{event.user_name}"
                            }
                        ]
                    }
                }
            },
        )

    @listen_to(r"^.*$")
    def handle_message(self, message: Message) -> None:
        if self.driver is None or not message.text:
            return

        bot_user_id = getattr(self.driver.client, "userid", None)
        if bot_user_id and message.user_id == bot_user_id:
            return

        mm_user_id: str = message.user_id
        rkey = object_name(self._secret, mm_user_id)
        extra = {"runtime_key": rkey, "channel_id": message.channel_id}

        service_dns = self._runtime.ensure_runtime(mm_user_id)

        if not self._runtime.is_ready(service_dns):
            post = self.driver.create_post(
                channel_id=message.channel_id,
                message="_Запускается сессия. Пожалуйста, подождите..._",
                root_id=message.reply_id,
            )
            post_id = post["id"]
            try:
                self._runtime.wait_ready(service_dns)
            except TimeoutError:
                logger.error("pod startup timeout for %s", rkey, extra=extra)
                self._patch(post_id, "Превышено время ожидания запуска сессии. Попробуйте ещё раз.")
                return
        else:
            post = self.driver.create_post(
                channel_id=message.channel_id,
                message=_CURSOR,
                root_id=message.reply_id,
            )
            post_id = post["id"]

        self._runtime.update_last_activity(mm_user_id)

        thread_id = message.reply_id or message.id
        ws_url = (
            f"ws://{service_dns}:{self._settings.zeroclaw_port}"
            f"/ws/chat?session_id={session_id(mm_user_id, thread_id)}"
        )

        def _on_approval_request(frame: dict) -> bool:
            return self._request_approval(
                frame,
                channel_id=message.channel_id,
                root_id=message.reply_id,
                main_post_id=post_id,
            )

        chunks: list[str] = []
        tool_log: list[str] = []  # persistent, accumulates all tool call/result lines
        current_tool: str = ""  # tool call pending its result (display string)
        current_tool_name: str = ""
        current_tool_key: str = ""
        last_update = time.monotonic()
        stream_start = time.monotonic()
        heartbeat_stop = threading.Event()

        def _render(cursor: bool = False) -> str:
            parts = []
            all_tools = tool_log + ([current_tool] if current_tool else [])
            if all_tools:
                parts.append("\n".join(all_tools))
            text = "".join(chunks)
            if text:
                parts.append(text + (_CURSOR if cursor else ""))
            return "\n\n".join(parts) if parts else ""

        def _heartbeat() -> None:
            tick = 0
            while not heartbeat_stop.wait(timeout=_HEARTBEAT_INTERVAL):
                if tool_log or current_tool or chunks:
                    continue
                elapsed = int(time.monotonic() - stream_start)
                dots = "." * (tick % 3 + 1)
                self._patch(post_id, f"_Думаю{dots}_ ({elapsed}с)")
                tick += 1

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        try:
            for frame in chat_stream(
                ws_url, message.text, on_approval_request=_on_approval_request
            ):
                ftype = frame.get("type")
                if ftype == "chunk":
                    chunks.append(frame.get("content", ""))
                    current_tool = ""
                    now = time.monotonic()
                    if now - last_update >= _UPDATE_INTERVAL:
                        self._patch(post_id, _render(cursor=True))
                        last_update = now
                elif ftype == "tool_call":
                    tool_name = frame.get("name", "?")
                    args = frame.get("args")
                    current_tool_name = tool_name
                    current_tool_key = _key_arg(tool_name, args)
                    current_tool = _fmt_tool_running(tool_name, args)
                    logger.info(
                        "tool_call tool=%s args=%s call_id=%s",
                        tool_name,
                        args,
                        frame.get("id"),
                        extra=extra,
                    )
                    self._patch(post_id, _render())
                    last_update = time.monotonic()
                elif ftype == "tool_result":
                    tool_name = frame.get("name", "?")
                    output = frame.get("output", "")
                    logger.info(
                        "tool_result tool=%s output=%s call_id=%s",
                        tool_name,
                        output[:200],
                        frame.get("id"),
                        extra=extra,
                    )
                    tool_log.append(
                        _fmt_tool_done(current_tool_name or tool_name, current_tool_key, output)
                    )
                    current_tool = ""
                    current_tool_name = ""
                    current_tool_key = ""
                    self._patch(post_id, _render())
                    last_update = time.monotonic()
                elif ftype == "done":
                    final = frame.get("full_response") or "".join(chunks)
                    tool_section = "\n".join(tool_log)
                    if tool_section and final:
                        self._patch(post_id, f"{tool_section}\n\n{final}")
                    elif tool_section:
                        self._patch(post_id, tool_section)
                    else:
                        self._patch(post_id, final)
                    break
                elif ftype == "error":
                    raise RuntimeError(f"ZeroClaw error: {frame.get('message')}")
                elif ftype in ("session_start", "approval_request"):
                    pass
                elif ftype is not None:
                    logger.warning("unhandled zeroclaw frame type=%s frame=%s", ftype, frame)
            else:
                final = _render()
                if final:
                    self._patch(post_id, final)
                else:
                    logger.warning(
                        "zeroclaw stream ended without 'done' frame for %s", rkey, extra=extra
                    )
                    self._patch(post_id, "Соединение с агентом прервано. Попробуйте ещё раз.")

        except Exception:
            logger.exception("zeroclaw chat failed for %s", rkey, extra=extra)
            self._patch(post_id, "Произошла ошибка при обработке запроса.")
            return
        finally:
            heartbeat_stop.set()
            hb.join(timeout=1)

        self._runtime.update_last_activity(mm_user_id)
