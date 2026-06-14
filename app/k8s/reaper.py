"""Background thread that scales down idle ZeroClaw pods."""

import logging
import threading
import time

from app import metrics
from app.config import Settings
from app.k8s.controller import RuntimeController

logger = logging.getLogger(__name__)


class IdleReaper(threading.Thread):
    def __init__(self, lifecycle: RuntimeController, settings: Settings) -> None:
        super().__init__(daemon=True, name="idle-reaper")
        self._lifecycle = lifecycle
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
            t0 = time.monotonic()
            try:
                idle = self._lifecycle.list_idle(self._settings.idle_timeout_seconds)
                for name in idle:
                    self._lifecycle.scale_down(name)
                    metrics.pods_reaped_total.inc()
                if idle:
                    logger.info("reaped %d idle runtimes", len(idle))
            except Exception:
                logger.exception("reaper iteration failed")
            finally:
                metrics.reaper_run_seconds.observe(time.monotonic() - t0)
