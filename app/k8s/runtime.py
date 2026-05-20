"""Per-user runtime lifecycle: ensure PVC + Deployment + Service, wait for readiness."""

import logging
import time
import urllib.request
from datetime import UTC, datetime

from kubernetes import client

from app import metrics
from app.config import Settings
from app.identity import object_name
from app.k8s.lifecycle import (  # noqa: F401 — re-exported for test imports
    ANNOTATION_LAST_ACTIVITY,
    LABEL_PART_OF,
    PART_OF_VALUE,
    LifecycleManager,
    _workspace_default,
    _workspace_default_data,
)
from app.k8s.user_state import UserStateManager

logger = logging.getLogger(__name__)


class RuntimeManager:
    def __init__(
        self,
        settings: Settings,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
    ) -> None:
        self._settings = settings
        self._core = core
        self._apps = apps
        self._secret = settings.k8s_name_secret.encode()
        self._ns = settings.k8s_namespace
        self._lifecycle = LifecycleManager(
            settings=settings,
            core=core,
            apps=apps,
            secret=self._secret,
            ns=self._ns,
        )
        self._user_state = UserStateManager(
            core=core,
            apps=apps,
            secret=self._secret,
            ns=self._ns,
            restart_fn=self._lifecycle.restart_if_running,
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def ensure_runtime(self, mm_user_id: str) -> str:
        return self._lifecycle.ensure_all(mm_user_id)

    def is_ready(self, service_dns: str) -> bool:
        url = f"http://{service_dns}:{self._settings.zeroclaw_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=1) as r:  # nosec B310
                return r.status == 200
        except Exception:
            logger.debug("health_check_failed service_dns=%s", service_dns, exc_info=True)
            return False

    def wait_ready(self, service_dns: str) -> None:
        """Poll /health until 200 or timeout. Raises TimeoutError."""
        s = self._settings
        t0 = time.monotonic()
        deadline = t0 + s.pod_ready_timeout_seconds
        url = f"http://{service_dns}:{s.zeroclaw_port}/health"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:  # nosec B310
                    if r.status == 200:
                        elapsed = time.monotonic() - t0
                        metrics.pod_startup_seconds.observe(elapsed)
                        logger.info(
                            "pod_ready service_dns=%s elapsed=%.1fs",
                            service_dns,
                            elapsed,
                            extra={"namespace": self._ns},
                        )
                        return
            except Exception:
                logger.debug("pod_not_ready_yet service_dns=%s", service_dns, exc_info=True)
            time.sleep(1.0)
        raise TimeoutError(
            f"ZeroClaw pod not ready after {s.pod_ready_timeout_seconds}s: {service_dns}"
        )

    def update_last_activity(self, mm_user_id: str) -> None:
        s = self._settings
        name = object_name(self._secret, mm_user_id)
        now = self._now_iso()
        try:
            self._apps.patch_namespaced_deployment(
                name,
                self._ns,
                {"metadata": {"annotations": {s.k8s_annotation_last_activity: now}}},
            )
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_UPDATE_LAST_ACTIVITY).inc()
            logger.warning(
                "failed to update last-activity for %s",
                name,
                exc_info=True,
                extra={"runtime_key": name, "namespace": self._ns, "mm_user_id": mm_user_id},
            )

    def list_idle(self, ttl_seconds: int) -> list[str]:
        """Return names of Deployments idle longer than ttl_seconds."""
        s = self._settings
        idle: list[str] = []
        try:
            deploys = self._apps.list_namespaced_deployment(
                self._ns,
                label_selector=f"{s.k8s_label_part_of}={s.k8s_part_of_value}",
            )
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_LIST_IDLE).inc()
            logger.error(
                "failed to list deployments for idle check",
                exc_info=True,
                extra={"namespace": self._ns},
            )
            return idle

        cutoff = time.time() - ttl_seconds
        running = 0
        for d in deploys.items:
            if (d.spec.replicas or 0) == 0:
                continue
            running += 1
            last = (d.metadata.annotations or {}).get(s.k8s_annotation_last_activity)
            if last:
                try:
                    ts = datetime.fromisoformat(last).timestamp()
                    if ts < cutoff:
                        idle.append(d.metadata.name)
                except ValueError:
                    pass
        metrics.active_pods.set(running)
        return idle

    def scale_down(self, name: str) -> None:
        self._lifecycle.scale_down(name)

    # ── user state delegation ─────────────────────────────────────────────────

    def set_user_env(self, mm_user_id: str, key: str, value: str) -> None:
        self._user_state.set_user_env(mm_user_id, key, value)

    def list_user_envs(self, mm_user_id: str) -> list[str]:
        return self._user_state.list_user_envs(mm_user_id)

    def delete_user_env(self, mm_user_id: str, key: str) -> bool:
        return self._user_state.delete_user_env(mm_user_id, key)

    def get_workspace_file(self, mm_user_id: str, filename: str) -> str | None:
        return self._user_state.get_workspace_file(mm_user_id, filename)

    def set_workspace_file(self, mm_user_id: str, filename: str, content: str) -> None:
        self._user_state.set_workspace_file(mm_user_id, filename, content)

    def reset_workspace_file(self, mm_user_id: str, filename: str) -> bool:
        return self._user_state.reset_workspace_file(mm_user_id, filename)

    # ── private method exposed for tests ─────────────────────────────────────

    def _ensure_identity_configmap(self, mm_user_id: str) -> None:
        self._lifecycle._ensure_identity_configmap(mm_user_id)
