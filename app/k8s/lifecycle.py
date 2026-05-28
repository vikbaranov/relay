import json
import logging
import pathlib
from datetime import UTC, datetime

from kubernetes import client

from app import metrics
from app.config import Settings
from app.identity import (
    env_secret_name,
    identity_configmap_name,
    object_name,
    pvc_name,
    zeroclaw_config_secret_name,
)

logger = logging.getLogger(__name__)

LABEL_PART_OF = "ai.relay.io/part-of"
ANNOTATION_LAST_ACTIVITY = "ai.relay.io/last-activity"
PART_OF_VALUE = "zeroclaw-runtime"

_WORKSPACE_DEFAULTS = pathlib.Path(__file__).parent.parent / "workspace"
WORKSPACE_FILES = ("SOUL.md", "IDENTITY.md")

_ALLOWED_COMMANDS = [
    "git",
    "ls",
    "cat",
    "grep",
    "find",
    "echo",
    "pwd",
    "wc",
    "head",
    "tail",
    "date",
    "df",
    "du",
    "uname",
    "uptime",
    "hostname",
    "gh",
    "rm",
    "mv",
    "cp",
    "mkdir",
    "touch",
    "bash",
    "curl",
    "zeroclaw",
]


def _workspace_default(filename: str) -> str | None:
    path = _WORKSPACE_DEFAULTS / filename
    if path.exists():
        return path.read_text()
    return None


def _workspace_default_data() -> dict[str, str]:
    data: dict[str, str] = {}
    for filename in WORKSPACE_FILES:
        content = _workspace_default(filename)
        if content is not None:
            data[filename] = content
    return data


class LifecycleManager:
    def __init__(
        self,
        settings: Settings,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        secret: bytes,
        ns: str,
    ) -> None:
        self._settings = settings
        self._core = core
        self._apps = apps
        self._secret = secret
        self._ns = ns
        self._configmap_ensured = False
        self._provider_secret_ensured = False

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def ensure_all(self, mm_user_id: str) -> str:
        """Create or wake up per-user K8s resources. Returns internal service DNS."""
        s = self._settings
        name = object_name(self._secret, mm_user_id)
        pvc = pvc_name(self._secret, mm_user_id)
        labels = {
            "app": name,
            s.k8s_label_part_of: s.k8s_part_of_value,
            s.k8s_label_mm_user: mm_user_id,
        }
        annotations: dict = {}

        self._ensure_configmap()
        self._ensure_provider_credentials_secret()
        self._ensure_identity_configmap(mm_user_id, labels, annotations)
        env_keys = self._get_user_env_keys(mm_user_id)
        self._ensure_user_zeroclaw_config(mm_user_id, env_keys, labels, annotations)
        self._ensure_pvc(pvc, labels, annotations)
        self._ensure_service(name, labels, annotations)
        self._ensure_deployment(name, pvc, mm_user_id, labels, annotations)
        return f"{name}.{self._ns}.svc.cluster.local"

    def scale_down(self, name: str) -> None:
        try:
            self._apps.patch_namespaced_deployment(name, self._ns, {"spec": {"replicas": 0}})
            logger.info(
                "scaled down idle runtime",
                extra={"runtime_key": name, "namespace": self._ns},
            )
        except Exception:
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_SCALE_DOWN).inc()
            logger.error(
                "failed to scale down %s",
                name,
                exc_info=True,
                extra={"runtime_key": name, "namespace": self._ns},
            )

    def restart_if_running(self, mm_user_id: str) -> None:
        name = object_name(self._secret, mm_user_id)
        env_keys = self._get_user_env_keys(mm_user_id)
        s = self._settings
        cname = zeroclaw_config_secret_name(self._secret, mm_user_id)
        try:
            self._core.patch_namespaced_secret(
                cname,
                self._ns,
                {"stringData": {s.zeroclaw_config_key: self._zeroclaw_config_toml(env_keys)}},
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                logger.warning(
                    "failed to update user zeroclaw config %s",
                    cname,
                    exc_info=True,
                    extra={"runtime_key": name, "namespace": self._ns, "mm_user_id": mm_user_id},
                )
        now = self._now_iso()
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
                extra={"runtime_key": name, "namespace": self._ns, "mm_user_id": mm_user_id},
            )

    def _ensure_configmap(self) -> None:
        if self._configmap_ensured:
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
        self._configmap_ensured = True

    def _zeroclaw_config_toml(self, env_keys: list[str]) -> str:
        s = self._settings
        allowed_commands = json.dumps(_ALLOWED_COMMANDS, indent=4)
        sections = [
            "[gateway]",
            "allow_public_bind = true",
            "",
            "[autonomy]",
            'level = "full"',
            "max_actions_per_hour = 100000",
            "",
            f"shell_env_passthrough = {json.dumps(env_keys)}",
            f"allowed_commands = {allowed_commands}",
            "",
            "[agent]",
            "max_tool_iterations = 150",
            "command_timeout = 180",
            "",
            "[http_request]",
            "allow_private_hosts = true",
            "",
            "[web_fetch]",
            'allowed_domain = ["*"]',
            'allowed_private_hosts = ["192.168.100.231", "192.168.0.0/16",'
            ' "172.16.0.0/12", "10.0.0.0/8"]',
            "",
            "[providers]",
            f'fallback = "{s.openai_base_url}"',
            "",
            f'[providers.models."{s.openai_base_url}"]',
            f'model = "{s.openai_model}"',
            f'api_key = "{s.openai_api_key}"',
            "",
        ]
        return "\n".join(sections)

    def _ensure_provider_credentials_secret(self) -> None:
        if self._provider_secret_ensured:
            return
        s = self._settings
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=s.zeroclaw_provider_credentials_secret,
                namespace=self._ns,
            ),
            string_data={s.zeroclaw_config_key: self._zeroclaw_config_toml([])},
        )
        try:
            self._core.read_namespaced_secret(s.zeroclaw_provider_credentials_secret, self._ns)
            self._core.replace_namespaced_secret(
                s.zeroclaw_provider_credentials_secret, self._ns, body
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENSURE_PROVIDER_CREDENTIALS).inc()
                raise
            self._core.create_namespaced_secret(self._ns, body)
            logger.info(
                "created provider credentials Secret %s", s.zeroclaw_provider_credentials_secret
            )
        self._provider_secret_ensured = True

    def _get_user_env_keys(self, mm_user_id: str) -> list[str]:
        sname = env_secret_name(self._secret, mm_user_id)
        try:
            sec = self._core.read_namespaced_secret(sname, self._ns)
            return list((sec.data or {}).keys())
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return []
            raise

    def _ensure_user_zeroclaw_config(
        self, mm_user_id: str, env_keys: list[str], labels: dict, annotations: dict
    ) -> None:
        s = self._settings
        name = zeroclaw_config_secret_name(self._secret, mm_user_id)
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self._ns,
                labels=labels,
                annotations=annotations,
            ),
            string_data={s.zeroclaw_config_key: self._zeroclaw_config_toml(env_keys)},
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
                extra={"namespace": self._ns, "mm_user_id": mm_user_id},
            )

    def _ensure_identity_configmap(self, mm_user_id: str, labels: dict, annotations: dict) -> None:
        """Create per-user identity ConfigMap pre-populated with global defaults, if absent."""
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
            extra={"namespace": self._ns, "mm_user_id": mm_user_id},
        )

    def _ensure_pvc(self, name: str, labels: dict, annotations: dict) -> None:
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
        logger.info("created PVC %s", name, extra={"runtime_key": name, "namespace": self._ns})

    def _ensure_service(self, name: str, labels: dict, annotations: dict) -> None:
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
        logger.info("created Service %s", name, extra={"runtime_key": name, "namespace": self._ns})

    def _ensure_deployment(
        self, name: str, pvc: str, mm_user_id: str, labels: dict, annotations: dict
    ) -> None:
        try:
            deploy = self._apps.read_namespaced_deployment(name, self._ns)
            self._ensure_deployment_config_volume(name, deploy, mm_user_id)
            if (deploy.spec.replicas or 0) == 0:
                now = self._now_iso()
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
                logger.info(
                    "scaled up idle runtime %s",
                    name,
                    extra={"runtime_key": name, "namespace": self._ns},
                )
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
            env=[
                client.V1EnvVar(name="ZEROCLAW_REQUIRE_PAIRING", value="false"),
            ],
            env_from=[
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(name=env_secret, optional=True)
                )
            ],
            volume_mounts=[
                client.V1VolumeMount(name="data", mount_path=s.zeroclaw_data_path),
                client.V1VolumeMount(
                    name="model-config", mount_path=s.zeroclaw_config_mount, read_only=True
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
                requests={"cpu": s.zeroclaw_cpu_request, "memory": s.zeroclaw_memory_request},
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
            extra={"runtime_key": name, "namespace": self._ns, "mm_user_id": mm_user_id},
        )

    def _ensure_deployment_config_volume(self, name: str, deploy, mm_user_id: str) -> None:
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
