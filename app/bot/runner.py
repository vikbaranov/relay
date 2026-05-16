import logging

from mmpy_bot import Bot
from mmpy_bot.settings import Settings as MmpySettings

from app import health
from app.config import Settings
from app.k8s.client import build_k8s_clients
from app.k8s.reaper import IdleReaper
from app.k8s.runtime import RuntimeManager
from app.bot.plugin import ZeroClawPlugin
from app.logging import configure_logging

logger = logging.getLogger(__name__)


def run_bot(settings: Settings) -> None:
    configure_logging(settings.log_level)

    health.start()

    core, apps = build_k8s_clients(
        mode=settings.k8s_mode,
        kubeconfig_path=settings.k8s_kubeconfig_path,
    )
    runtime = RuntimeManager(settings=settings, core=core, apps=apps)

    reaper = IdleReaper(runtime=runtime, settings=settings)
    reaper.start()

    plugin = ZeroClawPlugin(settings=settings, runtime=runtime)

    bot_settings = MmpySettings(
        MATTERMOST_URL=settings.mattermost_url,
        MATTERMOST_PORT=settings.mattermost_port,
        BOT_TOKEN=settings.mattermost_bot_token,
        BOT_TEAM=settings.mattermost_team,
        LOG_LEVEL=settings.log_level,
        SSL_VERIFY=settings.ssl_verify,
        WEBHOOK_HOST_ENABLED=True,
        WEBHOOK_HOST_URL="http://0.0.0.0",
        WEBHOOK_HOST_PORT=settings.webhook_host_port,
    )

    logger.info(
        "starting ops-agent",
        extra={
            "namespace": settings.k8s_namespace,
            "zeroclaw_image": settings.zeroclaw_image,
        },
    )

    health.mark_ready()

    bot = Bot(settings=bot_settings, plugins=[plugin], enable_logging=False)
    bot.run()
