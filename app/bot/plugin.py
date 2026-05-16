import logging
import threading

from mmpy_bot.function import listen_to, listen_webhook
from mmpy_bot.plugins.base import Plugin
from mmpy_bot.wrappers import ActionEvent, Message

from app.bot.formatting import _CURSOR, _MM_MAX_POST
from app.bot.stream_handler import StreamHandler
from app.config import Settings
from app.identity import object_name, session_id
from app.k8s.runtime import RuntimeManager
from app.zeroclaw.client import chat_stream

logger = logging.getLogger(__name__)


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
        try:
            self.driver.posts.patch_post(post_id, {"message": text})
        except Exception:
            logger.error("patch_post_failed post_id=%s", post_id, exc_info=True)

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
                            "text": f"**Подтверждение действия**\nИнструмент: `{tool}`\n```\n{summary}\n```",
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
        logger.info(
            "approval_requested request_id=%s tool=%s timeout_secs=%s",
            request_id, tool, timeout,
        )

        event = threading.Event()
        self._pending_approvals[request_id] = {
            "event": event,
            "approved": False,
            "approval_post_id": approval_post_id,
            "tool": tool,
            "summary": summary,
        }

        if event.wait(timeout=timeout):
            approved = self._pending_approvals.pop(request_id, {}).get("approved", False)
        else:
            self._pending_approvals.pop(request_id, None)
            approved = False
            logger.warning("approval_timeout request_id=%s tool=%s", request_id, tool)
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

        tool = pending.get("tool", "?")
        summary = pending.get("summary", "")
        pending["approved"] = approved
        pending["event"].set()

        logger.info(
            "approval_decision request_id=%s tool=%s approved=%s user=%s",
            request_id, tool, approved, event.user_name,
        )

        status = "✅ Разрешено" if approved else "❌ Отклонено"
        header = f"**Подтверждение действия**: `{tool}`"
        if summary:
            header += f"\n```\n{summary}\n```"
        self.driver.respond_to_web(
            event,
            {
                "update": {
                    "props": {
                        "attachments": [
                            {"text": f"{header}\n{status} пользователем @{event.user_name}"}
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
        thread_id = message.reply_id or message.id
        extra = {"runtime_key": rkey, "channel_id": message.channel_id, "thread_id": thread_id}
        logger.info("message_received runtime_key=%s thread_id=%s", rkey, thread_id, extra=extra)

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

        extra["post_id"] = post_id
        self._runtime.update_last_activity(mm_user_id)

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

        handler = StreamHandler(lambda text: self._patch(post_id, text), extra)
        heartbeat_stop = threading.Event()
        hb = threading.Thread(target=handler.heartbeat, args=(heartbeat_stop,), daemon=True)
        hb.start()
        try:
            for frame in chat_stream(
                ws_url, message.text, on_approval_request=_on_approval_request
            ):
                if handler.handle_frame(frame):
                    break
            else:
                handler.handle_stream_end(rkey)
        except Exception:
            logger.exception("zeroclaw chat failed for %s", rkey, extra=extra)
            self._patch(post_id, "Произошла ошибка при обработке запроса.")
            return
        finally:
            heartbeat_stop.set()
            hb.join(timeout=1)

        self._runtime.update_last_activity(mm_user_id)
        logger.info("message_done runtime_key=%s thread_id=%s", rkey, thread_id, extra=extra)
