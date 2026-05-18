import logging
import threading
import time
from dataclasses import dataclass

from mmpy_bot.function import listen_to, listen_webhook
from mmpy_bot.plugins.base import Plugin
from mmpy_bot.wrappers import ActionEvent, Message

from app import metrics
from app.bot.formatting import _CURSOR, _MM_MAX_POST
from app.bot.stream_handler import StreamHandler
from app.config import Settings
from app.identity import object_name, session_id
from app.k8s.runtime import RuntimeManager
from app.zeroclaw.client import ApprovalDecision, chat_stream

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    stop_event: threading.Event | None = None
    generation: int = 0


class ZeroClawPlugin(Plugin):
    def __init__(self, settings: Settings, runtime: RuntimeManager) -> None:
        super().__init__()
        self._settings = settings
        self._runtime = runtime
        self._secret = settings.k8s_name_secret.encode()
        self._sessions: dict[str, _SessionState] = {}
        self._pending_approvals: dict[str, dict] = {}

    def _reply_root_id(self, message: Message) -> str:
        if self._settings.mattermost_thread_replies:
            return message.root_id or message.id
        return ""

    def _session_scope(self, message: Message) -> str:
        root_id = self._reply_root_id(message)
        reply_target = f"{message.channel_id}:{root_id}" if root_id else message.channel_id
        return f"mattermost_{reply_target}_{message.user_id}"

    def _command(self, text: str) -> str:
        return text.strip().split(maxsplit=1)[0].lower()

    def _patch(self, post_id: str, text: str) -> None:
        if len(text) > _MM_MAX_POST:
            text = text[: _MM_MAX_POST - 60] + "\n\n_[ответ обрезан — слишком длинный]_"
        try:
            self.driver.posts.patch_post(post_id, {"message": text})
        except OSError:
            logger.error("patch_post_failed post_id=%s", post_id, exc_info=True)

    def _build_approval_payload(
        self,
        request_id: str,
        tool: str,
        summary: str,
        channel_id: str,
        root_id: str,
        main_post_id: str,
        webhook_url: str,
    ) -> dict:
        def _action(id_: str, name: str, decision: str) -> dict:
            return {
                "id": id_,
                "name": name,
                "type": "button",
                "integration": {
                    "url": webhook_url,
                    "context": {
                        "request_id": request_id,
                        "decision": decision,
                        "main_post_id": main_post_id,
                    },
                },
            }

        return {
            "channel_id": channel_id,
            "root_id": root_id,
            "props": {
                "attachments": [
                    {
                        "text": (
                            f"**Подтверждение действия**\nИнструмент: `{tool}`\n```\n{summary}\n```"
                        ),
                        "actions": [
                            _action("approve", "✅ Разрешить один раз", "approve"),
                            _action("always", "✅ Всегда разрешать", "always"),
                            _action("deny", "❌ Отклонить", "deny"),
                        ],
                    }
                ]
            },
        }

    def _request_approval(
        self, frame: dict, channel_id: str, root_id: str, main_post_id: str
    ) -> ApprovalDecision:
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
            options=self._build_approval_payload(
                request_id, tool, summary, channel_id, root_id, main_post_id, webhook_url
            )
        )
        approval_post_id = approval_post["id"]
        logger.info(
            "approval_requested request_id=%s tool=%s timeout_secs=%s",
            request_id,
            tool,
            timeout,
        )

        event = threading.Event()
        self._pending_approvals[request_id] = {
            "event": event,
            "decision": "deny",
            "approval_post_id": approval_post_id,
            "tool": tool,
            "summary": summary,
        }

        t0 = time.monotonic()
        if event.wait(timeout=timeout):
            decision = self._pending_approvals.pop(request_id, {}).get("decision", "deny")
            metrics.approvals_total.labels(decision=decision).inc()
        else:
            self._pending_approvals.pop(request_id, None)
            decision = "timeout"
            metrics.approvals_total.labels(decision="timeout").inc()
            logger.warning("approval_timeout request_id=%s tool=%s", request_id, tool)
            self.driver.posts.patch_post(
                approval_post_id,
                {
                    "props": {
                        "attachments": [{"text": "⏱ Таймаут. Действие отклонено автоматически."}]
                    }
                },
            )
        metrics.approval_wait_seconds.observe(time.monotonic() - t0)
        return decision

    @listen_webhook("approval")
    def handle_approval(self, event: ActionEvent) -> None:
        context = event.context or {}
        request_id = context.get("request_id")
        decision = context.get("decision")
        if decision not in ("approve", "deny", "always"):
            decision = "approve" if bool(context.get("approved", False)) else "deny"

        pending = self._pending_approvals.get(request_id)
        if not pending:
            self.driver.respond_to_web(
                event, {"update": {"message": "Запрос не найден или истёк."}}
            )
            return

        tool = pending.get("tool", "?")
        summary = pending.get("summary", "")
        pending["decision"] = decision
        pending["event"].set()

        logger.info(
            "approval_decision request_id=%s tool=%s decision=%s user=%s",
            request_id,
            tool,
            decision,
            event.user_name,
        )

        status = {
            "approve": "✅ Разрешено один раз",
            "always": "✅ Всегда разрешено",
            "deny": "❌ Отклонено",
        }[decision]
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

    def _handle_command(self, message: Message, scope: str, root_id: str) -> bool:
        command = self._command(message.text)
        if command == "!new":
            self._sessions.setdefault(scope, _SessionState()).generation += 1
            self.driver.create_post(
                channel_id=message.channel_id,
                message="Новый контекст начат для текущей ветки.",
                root_id=root_id,
            )
            return True
        if command == "!stop":
            state = self._sessions.get(scope)
            if state and state.stop_event:
                state.stop_event.set()
                self.driver.create_post(
                    channel_id=message.channel_id,
                    message="Выполнение остановлено.",
                    root_id=root_id,
                )
            else:
                self.driver.create_post(
                    channel_id=message.channel_id,
                    message="Нет активного выполнения.",
                    root_id=root_id,
                )
            return True
        return False

    def _run_stream(
        self,
        message: Message,
        scope: str,
        root_id: str,
        service_dns: str,
        post_id: str,
        rkey: str,
        t0: float,
        extra: dict,
    ) -> None:
        state = self._sessions.setdefault(scope, _SessionState())
        ws_url = (
            f"ws://{service_dns}:{self._settings.zeroclaw_port}"
            f"/ws/chat?session_id={session_id(scope, state.generation)}"
        )

        def _on_approval_request(frame: dict) -> ApprovalDecision:
            return self._request_approval(
                frame,
                channel_id=message.channel_id,
                root_id=root_id,
                main_post_id=post_id,
            )

        state.stop_event = threading.Event()
        stop_event = state.stop_event
        handler = StreamHandler(lambda text: self._patch(post_id, text), extra)
        heartbeat_stop = threading.Event()
        hb = threading.Thread(target=handler.heartbeat, args=(heartbeat_stop,), daemon=True)
        hb.start()
        metrics.active_clients.inc()
        try:
            for frame in chat_stream(
                ws_url, message.text, on_approval_request=_on_approval_request
            ):
                if stop_event.is_set():
                    break
                if handler.handle_frame(frame):
                    break
            else:
                handler.handle_stream_end(rkey)
        except (RuntimeError, OSError):
            metrics.messages_total.labels(outcome="error").inc()
            metrics.message_duration.observe(time.monotonic() - t0)
            logger.exception("zeroclaw chat failed for %s", rkey, extra=extra)
            self._patch(post_id, "Произошла ошибка при обработке запроса.")
            return
        finally:
            state.stop_event = None
            metrics.active_clients.dec()
            heartbeat_stop.set()
            hb.join(timeout=1)

        self._runtime.update_last_activity(message.user_id)
        metrics.messages_total.labels(outcome="success").inc()
        metrics.message_duration.observe(time.monotonic() - t0)
        logger.info("message_done runtime_key=%s session_scope=%s", rkey, scope, extra=extra)

    @listen_to(r"^.*$")
    def handle_message(self, message: Message) -> None:
        if self.driver is None or not message.text or not message.text.strip():
            return

        bot_user_id = getattr(self.driver.client, "userid", None)
        if bot_user_id and message.user_id == bot_user_id:
            return

        mm_user_id: str = message.user_id
        root_id = self._reply_root_id(message)
        scope = self._session_scope(message)

        if self._handle_command(message, scope, root_id):
            return

        rkey = object_name(self._secret, mm_user_id)
        t0 = time.monotonic()
        extra = {
            "runtime_key": rkey,
            "channel_id": message.channel_id,
            "session_scope": scope,
        }
        logger.info("message_received runtime_key=%s session_scope=%s", rkey, scope, extra=extra)

        service_dns = self._runtime.ensure_runtime(mm_user_id)

        if not self._runtime.is_ready(service_dns):
            post = self.driver.create_post(
                channel_id=message.channel_id,
                message="_Запускается сессия. Пожалуйста, подождите..._",
                root_id=root_id,
            )
            post_id = post["id"]
            try:
                self._runtime.wait_ready(service_dns)
            except TimeoutError:
                metrics.messages_total.labels(outcome="timeout").inc()
                metrics.message_duration.observe(time.monotonic() - t0)
                logger.error("pod startup timeout for %s", rkey, extra=extra)
                self._patch(post_id, "Превышено время ожидания запуска сессии. Попробуйте ещё раз.")
                return
        else:
            post = self.driver.create_post(
                channel_id=message.channel_id,
                message=_CURSOR,
                root_id=root_id,
            )
            post_id = post["id"]

        extra["post_id"] = post_id
        self._runtime.update_last_activity(mm_user_id)
        self._run_stream(message, scope, root_id, service_dns, post_id, rkey, t0, extra)
