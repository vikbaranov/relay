import hashlib
import logging
import threading
from datetime import UTC, datetime

from kubernetes import client

from app import metrics
from app.config import Settings
from app.identity import (
    env_secret_name,
    identity_configmap_name,
    object_name,
    zeroclaw_config_secret_name,
)
from app.k8s.config import ZeroClawConfigBuilder
from app.k8s.user_state import TOKEN_KEY
from app.k8s.workspace import WORKSPACE_FILES, _workspace_default_data

logger = logging.getLogger(__name__)


class ResourceProvisioner:
    def __init__(
        self,
        settings: Settings,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        secret: bytes,
        ns: str,
        config_builder: ZeroClawConfigBuilder,
    ) -> None:
        self._settings = settings
        self._core = core
        self._apps = apps
        self._secret = secret
        self._ns = ns
        self._config_builder = config_builder
        self._global_configmap_ensured = False
        self._provider_secret_ensured = False
        self._ensure_lock = threading.Lock()

    def ensure_global_configmap(self) -> None:
        with self._ensure_lock:
            if self._global_configmap_ensured:
                return
            s = self._settings
            data: dict[str, str] = _workspace_default_data()
            body = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=s.zeroclaw_configmap, namespace=self._ns),
                data=data,
            )
            try:
                self._core.read_namespaced_config_map(s.zeroclaw_configmap, self._ns)
                self._core.replace_namespaced_config_map(s.zeroclaw_configmap, self._ns, body)
            except client.exceptions.ApiException as exc:
                if exc.status != 404:
                    metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENSURE_CONFIGMAP).inc()
                    raise
                self._core.create_namespaced_config_map(self._ns, body)
                logger.info("created ConfigMap %s", s.zeroclaw_configmap)
            self._global_configmap_ensured = True

    def ensure_provider_credentials_secret(self) -> None:
        with self._ensure_lock:
            if self._provider_secret_ensured:
                return
            s = self._settings
            body = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=s.zeroclaw_provider_credentials_secret,
                    namespace=self._ns,
                ),
                string_data={s.zeroclaw_config_key: self._config_builder.build([])},
            )
            try:
                self._core.read_namespaced_secret(s.zeroclaw_provider_credentials_secret, self._ns)
                self._core.replace_namespaced_secret(
                    s.zeroclaw_provider_credentials_secret, self._ns, body
                )
            except client.exceptions.ApiException as exc:
                if exc.status != 404:
                    metrics.k8s_errors_total.labels(
                        op=metrics.K8S_OP_ENSURE_PROVIDER_CREDENTIALS
                    ).inc()
                    raise
                self._core.create_namespaced_secret(self._ns, body)
                logger.info(
                    "created provider credentials Secret %s",
                    s.zeroclaw_provider_credentials_secret,
                )
            self._provider_secret_ensured = True

    def ensure_identity_configmap(self, mm_user_id: str, labels: dict, annotations: dict) -> None:
        name = identity_configmap_name(self._secret, mm_user_id)
        defaults = _workspace_default_data()
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
            missing_defaults = {
                filename: content
                for filename, content in defaults.items()
                if filename not in (cm.data or {})
            }
            if missing_defaults:
                self._core.patch_namespaced_config_map(name, self._ns, {"data": missing_defaults})
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENSURE_IDENTITY_CONFIGMAP).inc()
                raise
        data = defaults
        self._core.create_namespaced_config_map(
            self._ns,
            client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=name,
                    namespace=self._ns,
                    labels=labels,
                    annotations=annotations,
                ),
                data=data,
            ),
        )
        logger.info(
            "created identity ConfigMap %s",
            name,
            extra={"mm_user_id": mm_user_id},
        )

    def ensure_user_config_secret(
        self,
        mm_user_id: str,
        env_keys: list[str],
        model: str,
        autonomy: str,
        api_key_override: str | None,
        labels: dict,
        annotations: dict,
    ) -> None:
        s = self._settings
        name = zeroclaw_config_secret_name(self._secret, mm_user_id)
        config_toml = self._config_builder.build(env_keys, model, autonomy, api_key_override)
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self._ns,
                labels=labels,
                annotations=annotations,
            ),
            string_data={s.zeroclaw_config_key: config_toml},
        )
        try:
            self._core.read_namespaced_secret(name, self._ns)
            self._core.replace_namespaced_secret(name, self._ns, body)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            self._core.create_namespaced_secret(self._ns, body)
            logger.info(
                "created user zeroclaw config Secret %s",
                name,
                extra={"mm_user_id": mm_user_id},
            )
        config_hash = hashlib.sha256(config_toml.encode()).hexdigest()[:12]
        annotations["ai.relay.io/config-hash"] = config_hash
        dep_name = object_name(self._secret, mm_user_id)
        try:
            self._apps.patch_namespaced_deployment(
                dep_name,
                self._ns,
                {
                    "spec": {
                        "template": {
                            "metadata": {"annotations": {"ai.relay.io/config-hash": config_hash}}
                        }
                    }
                },
            )
        except client.exceptions.ApiException:
            pass  # deployment may not exist yet on first ensure_all;
            # annotation will be in the create body

    def ensure_pvc(self, name: str, labels: dict, annotations: dict) -> None:
        try:
            self._core.read_namespaced_persistent_volume_claim(name, self._ns)
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENSURE_PVC).inc()
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
        logger.info("created PVC %s", name, extra={"runtime_key": name})

    def ensure_service(self, name: str, labels: dict, annotations: dict) -> None:
        try:
            self._core.read_namespaced_service(name, self._ns)
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENSURE_SERVICE).inc()
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
        logger.info("created Service %s", name, extra={"runtime_key": name})

    def ensure_deployment(
        self, name: str, pvc: str, mm_user_id: str, labels: dict, annotations: dict
    ) -> None:
        try:
            deploy = self._apps.read_namespaced_deployment(name, self._ns)
            self._patch_config_volume_if_stale(name, deploy, mm_user_id)
            if (deploy.spec.replicas or 0) == 0:
                now = datetime.now(UTC).isoformat()
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
                logger.info("scaled up idle runtime %s", name, extra={"runtime_key": name})
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENSURE_DEPLOYMENT).inc()
                raise

        s = self._settings
        env_secret = env_secret_name(self._secret, mm_user_id)
        sec_ctx = client.V1SecurityContext(
            run_as_non_root=True,
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=["ALL"]),
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        )
        container = client.V1Container(
            name="zeroclaw",
            image=s.zeroclaw_image,
            args=["daemon", "--host", "0.0.0.0"],  # nosec B104
            image_pull_policy="IfNotPresent",
            ports=[client.V1ContainerPort(container_port=s.zeroclaw_port, protocol="TCP")],
            env=[],
            env_from=[
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(name=env_secret, optional=True)
                )
            ],
            volume_mounts=[
                client.V1VolumeMount(name="data", mount_path=s.zeroclaw_data_path),
                client.V1VolumeMount(
                    name="model-config",
                    mount_path=f"{s.zeroclaw_config_mount}{s.zeroclaw_config_key}",
                    sub_path=s.zeroclaw_config_key,
                    read_only=True,
                ),
                *[
                    client.V1VolumeMount(
                        name="identity",
                        mount_path=f"{s.zeroclaw_data_path}/{f}",
                        sub_path=f,
                        read_only=True,
                    )
                    for f in WORKSPACE_FILES
                ],
            ],
            startup_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=s.zeroclaw_port),
                failure_threshold=15,
                period_seconds=1,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=s.zeroclaw_port),
                period_seconds=3,
            ),
            liveness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=s.zeroclaw_port),
                period_seconds=5,
                failure_threshold=3,
            ),
            security_context=sec_ctx,
            resources=client.V1ResourceRequirements(
                requests={
                    "cpu": s.zeroclaw_cpu_request,
                    "memory": s.zeroclaw_memory_request,
                },
                limits={"cpu": s.zeroclaw_cpu_limit, "memory": s.zeroclaw_memory_limit},
            ),
        )
        identity_cm = identity_configmap_name(self._secret, mm_user_id)
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
                    secret=client.V1SecretVolumeSource(
                        secret_name=zeroclaw_config_secret_name(self._secret, mm_user_id)
                    ),
                ),
                client.V1Volume(
                    name="identity",
                    config_map=client.V1ConfigMapVolumeSource(
                        name=identity_cm,
                        optional=True,
                    ),
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
        logger.info(
            "created Deployment %s",
            name,
            extra={"runtime_key": name, "mm_user_id": mm_user_id},
        )

    def _patch_config_volume_if_stale(self, name: str, deploy, mm_user_id: str) -> None:
        per_user_secret = zeroclaw_config_secret_name(self._secret, mm_user_id)
        template = getattr(deploy.spec, "template", None)
        pod_spec = getattr(template, "spec", None)
        volumes = getattr(pod_spec, "volumes", None)
        model_config = next((v for v in volumes or [] if v.name == "model-config"), None)
        if (
            model_config is not None
            and model_config.secret is not None
            and getattr(model_config.secret, "secret_name", None) == per_user_secret
        ):
            return
        self._apps.patch_namespaced_deployment(
            name,
            self._ns,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [
                                {
                                    "name": "model-config",
                                    "secret": {"secretName": per_user_secret},
                                    "configMap": None,
                                }
                            ]
                        }
                    }
                }
            },
        )

    def get_user_env_keys(self, mm_user_id: str) -> list[str]:
        sname = env_secret_name(self._secret, mm_user_id)
        try:
            sec = self._core.read_namespaced_secret(sname, self._ns)
            return sorted(k for k in (sec.data or {}).keys() if k != TOKEN_KEY)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return []
            raise
