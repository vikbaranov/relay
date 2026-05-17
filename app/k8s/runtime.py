"""Per-user runtime lifecycle: ensure PVC + Deployment + Service, wait for readiness."""

import logging
import time
import urllib.request
from datetime import datetime, timezone

from kubernetes import client

from app import metrics
from app.config import Settings
from app.identity import object_name, pvc_name

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

    def ensure_runtime(self, mm_user_id: str) -> str:
        """Create or wake up per-user resources. Returns internal service DNS."""
        s = self._settings
        name = object_name(self._secret, mm_user_id)
        pvc = pvc_name(self._secret, mm_user_id)
        labels = {"app": name, s.k8s_label_part_of: s.k8s_part_of_value}
        annotations = {s.k8s_annotation_mm_user: mm_user_id}

        self._ensure_configmap()
        self._ensure_pvc(pvc, labels, annotations)
        self._ensure_service(name, labels, annotations)
        self._ensure_deployment(name, pvc, mm_user_id, labels, annotations)
        return f"{name}.{self._ns}.svc.cluster.local"

    def is_ready(self, service_dns: str) -> bool:
        url = f"http://{service_dns}:{self._settings.zeroclaw_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
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
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        elapsed = time.monotonic() - t0
                        metrics.pod_startup_seconds.observe(elapsed)
                        logger.info(
                            "pod_ready service_dns=%s elapsed=%.1fs",
                            service_dns, elapsed,
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
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._apps.patch_namespaced_deployment(
                name,
                self._ns,
                {"metadata": {"annotations": {s.k8s_annotation_last_activity: now}}},
            )
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_UPDATE_LAST_ACTIVITY).inc()
            logger.warning(
                "failed to update last-activity for %s", name,
                exc_info=True, extra={"runtime_key": name, "namespace": self._ns},
            )

    def list_idle(self, ttl_seconds: int) -> list[str]:
        """Return names of Deployments idle longer than ttl_seconds."""
        s = self._settings
        idle = []
        try:
            deploys = self._apps.list_namespaced_deployment(
                self._ns,
                label_selector=f"{s.k8s_label_part_of}={s.k8s_part_of_value}",
            )
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_LIST_IDLE).inc()
            logger.warning(
                "failed to list deployments for idle check",
                exc_info=True, extra={"namespace": self._ns},
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
        try:
            self._apps.patch_namespaced_deployment(name, self._ns, {"spec": {"replicas": 0}})
            logger.info("scaled down idle runtime", extra={"runtime_key": name})
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_SCALE_DOWN).inc()
            logger.warning(
                "failed to scale down %s", name,
                exc_info=True, extra={"runtime_key": name, "namespace": self._ns},
            )

    def _ensure_configmap(self) -> None:
        s = self._settings
        toml = f"""\
[gateway]
allow_public_bind = true

[agent]
max_tool_iterations = 30
parallel_tools = true

[providers]
fallback = "{s.openai_base_url}"

[providers.models."{s.openai_base_url}"]
model = "{s.openai_model}"
api_key = "{s.openai_api_key}"
"""
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=s.zeroclaw_configmap, namespace=self._ns),
            data={s.zeroclaw_config_key: toml},
        )
        try:
            self._core.read_namespaced_config_map(s.zeroclaw_configmap, self._ns)
            self._core.replace_namespaced_config_map(s.zeroclaw_configmap, self._ns, body)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            self._core.create_namespaced_config_map(self._ns, body)
            logger.info("created ConfigMap %s", s.zeroclaw_configmap)

    def _ensure_pvc(
        self,
        name: str,
        labels: dict,
        annotations: dict,
    ) -> None:
        try:
            self._core.read_namespaced_persistent_volume_claim(name, self._ns)
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        s = self._settings
        spec = client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(requests={"storage": s.user_pvc_size}),
        )
        if s.user_pvc_storage_class:
            spec.storage_class_name = s.user_pvc_storage_class

        self._core.create_namespaced_persistent_volume_claim(
            self._ns,
            client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name=name, labels=labels, annotations=annotations),
                spec=spec,
            ),
        )
        logger.info("created PVC %s", name)

    def _ensure_service(
        self,
        name: str,
        labels: dict,
        annotations: dict,
    ) -> None:
        try:
            self._core.read_namespaced_service(name, self._ns)
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        s = self._settings
        self._core.create_namespaced_service(
            self._ns,
            client.V1Service(
                metadata=client.V1ObjectMeta(name=name, labels=labels, annotations=annotations),
                spec=client.V1ServiceSpec(
                    selector={"app": name},
                    ports=[
                        client.V1ServicePort(
                            port=s.zeroclaw_port,
                            target_port=s.zeroclaw_port,
                            protocol="TCP",
                        )
                    ],
                    type="ClusterIP",
                ),
            ),
        )
        logger.info("created Service %s", name)

    def _ensure_deployment(
        self,
        name: str,
        pvc: str,
        mm_user_id: str,
        labels: dict,
        annotations: dict,
    ) -> None:
        try:
            deploy = self._apps.read_namespaced_deployment(name, self._ns)
            if (deploy.spec.replicas or 0) == 0:
                now = datetime.now(timezone.utc).isoformat()
                self._apps.patch_namespaced_deployment(
                    name,
                    self._ns,
                    {
                        "spec": {"replicas": 1},
                        "metadata": {
                            "annotations": {self._settings.k8s_annotation_last_activity: now}
                        },
                    },
                )
                logger.info("scaled up idle runtime %s", name)
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        s = self._settings
        sec_ctx = client.V1SecurityContext(
            run_as_non_root=True,
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=["ALL"]),
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        )
        container = client.V1Container(
            name="zeroclaw",
            image=s.zeroclaw_image,
            args=["daemon", "--host", "0.0.0.0"],
            image_pull_policy="IfNotPresent",
            ports=[client.V1ContainerPort(container_port=s.zeroclaw_port, protocol="TCP")],
            env=[
                client.V1EnvVar(name="ZEROCLAW_REQUIRE_PAIRING", value="false"),
            ],
            volume_mounts=[
                client.V1VolumeMount(name="data", mount_path=s.zeroclaw_data_path),
                client.V1VolumeMount(
                    name="model-config", mount_path=s.zeroclaw_config_mount, read_only=True
                ),
            ],
            startup_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=s.zeroclaw_port),
                failure_threshold=30,
                period_seconds=2,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=s.zeroclaw_port),
                initial_delay_seconds=5,
                period_seconds=5,
            ),
            liveness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=s.zeroclaw_port),
                initial_delay_seconds=30,
                period_seconds=15,
                failure_threshold=3,
            ),
            security_context=sec_ctx,
            resources=client.V1ResourceRequirements(
                requests={"cpu": s.zeroclaw_cpu_request, "memory": s.zeroclaw_memory_request},
                limits={"cpu": s.zeroclaw_cpu_limit, "memory": s.zeroclaw_memory_limit},
            ),
        )
        pod_spec = client.V1PodSpec(
            automount_service_account_token=False,
            containers=[container],
            volumes=[
                client.V1Volume(
                    name="data",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=pvc
                    ),
                ),
                client.V1Volume(
                    name="model-config",
                    config_map=client.V1ConfigMapVolumeSource(name=s.zeroclaw_configmap),
                ),
            ],
        )
        self._apps.create_namespaced_deployment(
            self._ns,
            client.V1Deployment(
                metadata=client.V1ObjectMeta(name=name, labels=labels, annotations=annotations),
                spec=client.V1DeploymentSpec(
                    replicas=1,
                    selector=client.V1LabelSelector(match_labels={"app": name}),
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(labels=labels, annotations=annotations),
                        spec=pod_spec,
                    ),
                ),
            ),
        )
        logger.info("created Deployment %s for user %s", name, mm_user_id[:8] + "…")
