import logging

from mmpy_bot.function import listen_to
from mmpy_bot.plugins.base import Plugin
from mmpy_bot.wrappers import Message

from app.config import Settings
from app.identity import object_name, session_id
from app.k8s.runtime import RuntimeManager
from app.zeroclaw.client import chat

logger = logging.getLogger(__name__)


class ZeroClawPlugin(Plugin):
    def __init__(self, settings: Settings, runtime: RuntimeManager) -> None:
        super().__init__()
        self._settings = settings
        self._runtime = runtime
        self._secret = settings.k8s_name_secret.encode()

    @listen_to(r"^.*$")
    def handle_message(self, message: Message) -> None:
        if self.driver is None or not message.text:
            return

        # Ignore bot's own messages
        bot_user_id = getattr(self.driver.client, "userid", None)
        if bot_user_id and message.user_id == bot_user_id:
            return

        mm_user_id: str = message.user_id
        rkey = object_name(self._secret, mm_user_id)
        extra = {"runtime_key": rkey, "channel_id": message.channel_id}

        service_dns = self._runtime.ensure_runtime(mm_user_id)

        if not self._runtime.is_ready(service_dns):
            self.driver.reply_to(message, "_Starting your session…_")
            try:
                self._runtime.wait_ready(service_dns)
            except TimeoutError:
                logger.error("pod startup timeout for %s", rkey, extra=extra)
                self.driver.reply_to(message, "Session startup timed out. Please retry.")
                return

        self._runtime.update_last_activity(mm_user_id)

        ws_url = (
            f"ws://{service_dns}:{self._settings.zeroclaw_port}"
            f"/ws/chat?session_id={session_id(mm_user_id)}"
        )

        try:
            response = chat(ws_url, message.text)
        except Exception:
            logger.exception("zeroclaw chat failed for %s", rkey, extra=extra)
            self.driver.reply_to(message, "An error occurred processing your request.")
            return

        self._runtime.update_last_activity(mm_user_id)
        self.driver.reply_to(message, response)
