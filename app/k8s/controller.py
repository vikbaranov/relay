import logging
import time
import urllib.request
from datetime import UTC, datetime

from kubernetes import client

from app import metrics
from app.config import Settings
from app.identity import object_name, pvc_name, zeroclaw_config_secret_name
from app.k8s.config import ZeroClawConfigBuilder
from app.k8s.provisioner import ResourceProvisioner
from app.k8s.user_state import UserStateManager

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RuntimeController:
    def __init__(
        self,
        settings: Settings,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        secret: bytes,
        ns: str,
        provisioner: ResourceProvisioner,
        user_state: UserStateManager,
        config_builder: ZeroClawConfigBuilder,
    ) -> None:
        self._settings = settings
        self._core = core
        self._apps = apps
        self._secret = secret
        self._ns = ns
        self._provisioner = provisioner
        self._user_state = user_state
        self._config_builder = config_builder

    def ensure_all(self, mm_user_id: str, *, model_user_id: str | None = None) -> str:
        s = self._settings
        name = object_name(self._secret, mm_user_id)
        pvc = pvc_name(self._secret, mm_user_id)
        labels = {
            "app": name,
            s.k8s_label_part_of: s.k8s_part_of_value,
            s.k8s_label_mm_user: mm_user_id,
        }
        annotations: dict = {}

        self._provisioner.ensure_global_configmap()
        self._provisioner.ensure_provider_credentials_secret()
        self._provisioner.ensure_identity_configmap(mm_user_id, labels, annotations)

        env_keys = self._provisioner.get_user_env_keys(mm_user_id)
        model = self._user_state.get_user_model(model_user_id or mm_user_id)
        autonomy = self._user_state.get_user_autonomy(mm_user_id)
        api_key_override = self._user_state.get_user_token(mm_user_id)

        self._provisioner.ensure_user_config_secret(
            mm_user_id, env_keys, model, autonomy, api_key_override, labels, annotations
        )
        self._provisioner.ensure_pvc(pvc, labels, annotations)
        self._provisioner.ensure_service(name, labels, annotations)
        self._provisioner.ensure_deployment(name, pvc, mm_user_id, labels, annotations)
        return f"{name}.{self._ns}.svc.cluster.local"

    def scale_down(self, name: str) -> None:
        try:
            self._apps.patch_namespaced_deployment(name, self._ns, {"spec": {"replicas": 0}})
            logger.info("scaled down idle runtime", extra={"runtime_key": name})
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_SCALE_DOWN).inc()
            logger.error(
                "failed to scale down %s", name, exc_info=True, extra={"runtime_key": name}
            )

    def restart_if_running(self, mm_user_id: str, *, model_user_id: str | None = None) -> None:
        name = object_name(self._secret, mm_user_id)
        env_keys = self._provisioner.get_user_env_keys(mm_user_id)
        model = self._user_state.get_user_model(model_user_id or mm_user_id)
        autonomy = self._user_state.get_user_autonomy(mm_user_id)
        api_key_override = self._user_state.get_user_token(mm_user_id)
        s = self._settings
        cname = zeroclaw_config_secret_name(self._secret, mm_user_id)
        config_toml = self._config_builder.build(env_keys, model, autonomy, api_key_override)
        try:
            self._core.patch_namespaced_secret(
                cname,
                self._ns,
                {"stringData": {s.zeroclaw_config_key: config_toml}},
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENV_RESTART).inc()
                logger.warning(
                    "failed to update user zeroclaw config %s",
                    cname,
                    exc_info=True,
                    extra={"runtime_key": name, "mm_user_id": mm_user_id},
                )
                raise
        now = _now_iso()
        try:
            self._apps.patch_namespaced_deployment(
                name,
                self._ns,
                {
                    "spec": {
                        "template": {
                            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                        }
                    }
                },
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENV_RESTART).inc()
            logger.error(
                "failed to restart deployment %s",
                name,
                exc_info=True,
                extra={"runtime_key": name, "mm_user_id": mm_user_id},
            )
            raise

    def is_ready(self, service_dns: str) -> bool:
        url = f"http://{service_dns}:{self._settings.zeroclaw_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=1) as r:  # nosec B310
                return r.status == 200
        except Exception:
            logger.debug("health_check_failed service_dns=%s", service_dns, exc_info=True)
            return False

    def wait_ready(self, service_dns: str) -> None:
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
                        logger.info("pod_ready service_dns=%s elapsed=%.1fs", service_dns, elapsed)
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
        now = _now_iso()
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
                extra={"runtime_key": name, "mm_user_id": mm_user_id},
            )

    def list_idle(self, ttl_seconds: int) -> list[str]:
        s = self._settings
        idle: list[str] = []
        try:
            deploys = self._apps.list_namespaced_deployment(
                self._ns,
                label_selector=f"{s.k8s_label_part_of}={s.k8s_part_of_value}",
            )
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_LIST_IDLE).inc()
            logger.error("failed to list deployments for idle check", exc_info=True)
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
