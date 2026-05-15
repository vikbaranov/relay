import logging
import time

from mmpy_bot.function import listen_to
from mmpy_bot.plugins.base import Plugin
from mmpy_bot.wrappers import Message

from app.config import Settings
from app.identity import object_name, session_id
from app.k8s.runtime import RuntimeManager
from app.zeroclaw.client import chat_stream

logger = logging.getLogger(__name__)

_CURSOR = "▌"
_UPDATE_INTERVAL = 1.0  # seconds between incremental patches


class ZeroClawPlugin(Plugin):
    def __init__(self, settings: Settings, runtime: RuntimeManager) -> None:
        super().__init__()
        self._settings = settings
        self._runtime = runtime
        self._secret = settings.k8s_name_secret.encode()

    def _patch(self, post_id: str, text: str) -> None:
        self.driver.posts.patch_post(post_id, {"message": text})

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

        post = self.driver.create_post(
            channel_id=message.channel_id,
            message="_Запускается сессия. Пожалуйста, подождите_",
            root_id=message.reply_id,
        )
        post_id = post["id"]

        service_dns = self._runtime.ensure_runtime(mm_user_id)

        if not self._runtime.is_ready(service_dns):
            self._patch(post_id, "_Запуск контейнера. Пожалуйста, подождите..._")
            try:
                self._runtime.wait_ready(service_dns)
            except TimeoutError:
                logger.error("pod startup timeout for %s", rkey, extra=extra)
                self._patch(post_id, "Превышено время ожидания запуска сессии. Попробуйте ещё раз.")
                return

        self._runtime.update_last_activity(mm_user_id)

        ws_url = (
            f"ws://{service_dns}:{self._settings.zeroclaw_port}"
            f"/ws/chat?session_id={session_id(mm_user_id)}"
        )

        try:
            chunks: list[str] = []
            last_update = time.monotonic()

            for frame in chat_stream(ws_url, message.text):
                ftype = frame.get("type")
                if ftype == "chunk":
                    chunks.append(frame.get("content", ""))
                    now = time.monotonic()
                    if now - last_update >= _UPDATE_INTERVAL:
                        self._patch(post_id, "".join(chunks) + _CURSOR)
                        last_update = now
                elif ftype == "done":
                    final = frame.get("full_response") or "".join(chunks)
                    self._patch(post_id, final)
                    break
                elif ftype == "error":
                    raise RuntimeError(f"ZeroClaw error: {frame.get('message')}")
                elif ftype is not None:
                    logger.warning("unhandled zeroclaw frame type=%s frame=%s", ftype, frame)
            else:
                final = "".join(chunks)
                if final:
                    self._patch(post_id, final)

        except Exception:
            logger.exception("zeroclaw chat failed for %s", rkey, extra=extra)
            self._patch(post_id, "Произошла ошибка при обработке запроса.")
            return

        self._runtime.update_last_activity(mm_user_id)
