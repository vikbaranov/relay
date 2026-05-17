"""Background thread that scales down idle ZeroClaw pods."""

import logging
import threading

from app import metrics
from app.config import Settings
from app.k8s.runtime import RuntimeManager

logger = logging.getLogger(__name__)


class IdleReaper(threading.Thread):
    def __init__(self, runtime: RuntimeManager, settings: Settings) -> None:
        super().__init__(daemon=True, name="idle-reaper")
        self._runtime = runtime
        self._settings = settings
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.info(
            "idle reaper started (ttl=%ds interval=%ds)",
            self._settings.idle_timeout_seconds,
            self._settings.reaper_interval_seconds,
        )
        while not self._stop.wait(self._settings.reaper_interval_seconds):
            try:
                idle = self._runtime.list_idle(self._settings.idle_timeout_seconds)
                for name in idle:
                    self._runtime.scale_down(name)
                    metrics.pods_reaped_total.inc()
                if idle:
                    logger.info("reaped %d idle runtimes", len(idle))
            except Exception:
                logger.exception("reaper iteration failed")
