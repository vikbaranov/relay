import logging
import threading
import time

from mmpy_bot.function import listen_to, listen_webhook
from mmpy_bot.plugins.base import Plugin
from mmpy_bot.wrappers import ActionEvent, Message

from app import metrics
from app.bot.approval import ApprovalDecision, ApprovalManager
from app.bot.commands import CommandHandler, _SessionState
from app.bot.dialogs import DialogHandler
from app.bot.formatting import _CURSOR, patch_post
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
        self._sessions: dict[str, _SessionState] = {}
        self._base_url: str = (
            settings.webhook_public_url or f"http://localhost:{settings.webhook_host_port}"
        )
        self._approval = ApprovalManager(get_driver=lambda: self.driver, base_url=self._base_url)
        self._commands = CommandHandler(
            get_driver=lambda: self.driver,
            runtime=self._runtime,
            base_url=self._base_url,
            allowed_models=settings.allowed_models,
            sessions=self._sessions,
        )
        self._dialogs = DialogHandler(
            get_driver=lambda: self.driver,
            runtime=self._runtime,
            base_url=self._base_url,
        )

    # ── backward-compat shims used by tests ───────────────────────────────────

    @property
    def _pending_approvals(self) -> dict:
        return self._approval._pending

    def _request_approval(
        self, frame: dict, channel_id: str, root_id: str, main_post_id: str
    ) -> ApprovalDecision:
        return self._approval.request(frame, channel_id, root_id, main_post_id)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _reply_root_id(self, message: Message) -> str:
        if self._settings.mattermost_thread_replies:
            return message.root_id or message.id
        return ""

    def _session_scope(self, message: Message, is_channel: bool = False) -> str:
        root_id = self._reply_root_id(message)
        reply_target = f"{message.channel_id}:{root_id}" if root_id else message.channel_id
        if is_channel:
            return f"mattermost_channel_{reply_target}"
        return f"mattermost_{reply_target}_{message.user_id}"

    # ── webhook handlers ───────────────────────────────────────────────────────

    @listen_webhook("approval")
    def handle_approval(self, event: ActionEvent) -> None:
        response = self._approval.resolve(event)
        if response is None:
            self.driver.respond_to_web(
                event, {"update": {"message": "Запрос не найден или истёк."}}
            )
            return
        self.driver.respond_to_web(event, response)

    @listen_webhook("workspace_file_dialog")
    def handle_workspace_file_dialog(self, event: ActionEvent) -> None:
        self._dialogs.open_workspace_file_dialog(event)

    @listen_webhook("workspace_file_submit")
    def handle_workspace_file_submit(self, event: ActionEvent) -> None:
        self._dialogs.submit_workspace_file(event)

    @listen_webhook("env_set_dialog")
    def handle_env_set_dialog(self, event: ActionEvent) -> None:
        self._dialogs.open_env_set_dialog(event)

    @listen_webhook("env_set_submit")
    def handle_env_set_submit(self, event: ActionEvent) -> None:
        self._dialogs.submit_env_set(event)

    # ── streaming ──────────────────────────────────────────────────────────────

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
        runtime_key: str,
    ) -> None:
        state = self._sessions.setdefault(scope, _SessionState())
        sid = session_id(scope, state.generation)
        ws_url = f"ws://{service_dns}:{self._settings.zeroclaw_port}/ws/chat?session_id={sid}"
        extra["session_id"] = sid

        def _on_approval_request(frame: dict) -> ApprovalDecision:
            return self._approval.request(
                frame,
                channel_id=message.channel_id,
                root_id=root_id,
                main_post_id=post_id,
            )

        state.stop_event = threading.Event()
        stop_event = state.stop_event
        handler = StreamHandler(lambda text: patch_post(self.driver, post_id, text), extra)
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
            metrics.message_duration.labels(outcome="error").observe(time.monotonic() - t0)
            logger.exception("zeroclaw chat failed for %s", rkey, extra=extra)
            patch_post(self.driver, post_id, "Произошла ошибка при обработке запроса.")
            return
        finally:
            state.stop_event = None
            metrics.active_clients.dec()
            heartbeat_stop.set()
            hb.join(timeout=1)

        self._runtime.update_last_activity(runtime_key)
        metrics.messages_total.labels(outcome="success").inc()
        metrics.message_duration.labels(outcome="success").observe(time.monotonic() - t0)
        logger.info("message_done", extra=extra)

    def _handle_request(self, message: Message, runtime_key: str, is_channel: bool = False) -> None:
        if self.driver is None or not message.text or not message.text.strip():
            return

        bot_user_id = getattr(self.driver.client, "userid", None)
        if bot_user_id and message.user_id == bot_user_id:
            return

        root_id = (message.root_id or message.id) if is_channel else self._reply_root_id(message)
        scope = self._session_scope(message, is_channel=is_channel)

        if self._commands.handle(message, scope, root_id):
            return

        rkey = object_name(self._secret, runtime_key)
        t0 = time.monotonic()
        extra = {
            "runtime_key": rkey,
            "mm_user_id": message.user_id,
            "channel_id": message.channel_id,
        }
        logger.info("message_received", extra=extra)

        post = self.driver.create_post(
            channel_id=message.channel_id,
            message="_Запрос получен. Готовлю сессию..._",
            root_id=root_id,
        )
        post_id = post["id"]

        t_ensure = time.monotonic()
        service_dns = self._runtime.ensure_runtime(runtime_key)
        metrics.ensure_runtime_seconds.observe(time.monotonic() - t_ensure)

        if not self._runtime.is_ready(service_dns):
            try:
                t_ready = time.monotonic()
                self._runtime.wait_ready(service_dns)
                ready_elapsed = round(time.monotonic() - t_ready)
                patch_post(self.driver, post_id, f"Готов. Заняло {ready_elapsed}с")
            except TimeoutError:
                metrics.messages_total.labels(outcome="timeout").inc()
                metrics.message_duration.labels(outcome="timeout").observe(time.monotonic() - t0)
                logger.error("pod startup timeout for %s", rkey, extra=extra)
                patch_post(
                    self.driver,
                    post_id,
                    "Превышено время ожидания запуска сессии. Попробуйте ещё раз.",
                )
                return
        else:
            patch_post(self.driver, post_id, _CURSOR)

        extra["post_id"] = post_id
        self._run_stream(
            message, scope, root_id, service_dns, post_id, rkey, t0, extra, runtime_key
        )

    @listen_to(r"^.*$", direct_only=True)
    def handle_dm(self, message: Message) -> None:
        self._handle_request(message, runtime_key=message.user_id)

    @listen_to(r"^.*$", needs_mention=True)
    def handle_channel_mention(self, message: Message) -> None:
        if message.is_direct_message:
            return
        self._handle_request(message, runtime_key=message.channel_id, is_channel=True)
