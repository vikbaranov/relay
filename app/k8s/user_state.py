import base64
import logging

from kubernetes import client

from app import metrics
from app.identity import env_secret_name, identity_configmap_name
from app.k8s.workspace import _workspace_default, _workspace_default_data

logger = logging.getLogger(__name__)

MODEL_KEY = "MODEL"
AUTONOMY_KEY = "AUTONOMY"
_AUTONOMY_LEVELS = ("full", "supervised")
DEFAULT_AUTONOMY = "supervised"
TOKEN_KEY = "OPENAI_API_KEY_OVERRIDE"


class UserStateManager:
    def __init__(
        self,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        secret: bytes,
        ns: str,
        allowed_models: list[str],
    ) -> None:
        self._core = core
        self._apps = apps
        self._secret = secret
        self._ns = ns
        self._allowed_models = allowed_models

    @property
    def default_model(self) -> str:
        return self._allowed_models[0]

    @property
    def default_autonomy(self) -> str:
        return DEFAULT_AUTONOMY

    def get_user_model(self, mm_user_id: str) -> str:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
            model = (cm.data or {}).get(MODEL_KEY)
            if model in self._allowed_models:
                return model
            return self.default_model
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return self.default_model
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_WORKSPACE_FILE_GET).inc()
            raise

    def set_user_model(self, mm_user_id: str, model: str) -> bool:
        if model not in self._allowed_models:
            return False
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            self._core.patch_namespaced_config_map(name, self._ns, {"data": {MODEL_KEY: model}})
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_WORKSPACE_FILE_SET).inc()
                raise
            self._core.create_namespaced_config_map(
                self._ns,
                client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=name, namespace=self._ns),
                    data={**_workspace_default_data(), MODEL_KEY: model},
                ),
            )
        return True

    def reset_user_model(self, mm_user_id: str) -> bool:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
            data = cm.data or {}
            if MODEL_KEY not in data:
                return False
            self._core.patch_namespaced_config_map(name, self._ns, {"data": {MODEL_KEY: None}})
            return True
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_WORKSPACE_FILE_RESET).inc()
            raise

    def get_user_autonomy(self, mm_user_id: str) -> str:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return DEFAULT_AUTONOMY
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_AUTONOMY_GET).inc()
            raise
        level = (cm.data or {}).get(AUTONOMY_KEY)
        if level in _AUTONOMY_LEVELS:
            return level
        return DEFAULT_AUTONOMY

    def set_user_autonomy(self, mm_user_id: str, level: str) -> bool:
        if level not in _AUTONOMY_LEVELS:
            return False
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            self._core.patch_namespaced_config_map(name, self._ns, {"data": {AUTONOMY_KEY: level}})
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_AUTONOMY_SET).inc()
                raise
            self._core.create_namespaced_config_map(
                self._ns,
                client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=name, namespace=self._ns),
                    data={**_workspace_default_data(), AUTONOMY_KEY: level},
                ),
            )
        return True

    def reset_user_autonomy(self, mm_user_id: str) -> bool:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_AUTONOMY_RESET).inc()
            raise
        data = cm.data or {}
        if AUTONOMY_KEY not in data:
            return False
        self._core.patch_namespaced_config_map(name, self._ns, {"data": {AUTONOMY_KEY: None}})
        return True

    def set_user_env(self, mm_user_id: str, key: str, value: str) -> None:
        sname = env_secret_name(self._secret, mm_user_id)
        try:
            self._core.patch_namespaced_secret(sname, self._ns, {"stringData": {key: value}})
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENV_SET).inc()
                raise
            self._core.create_namespaced_secret(
                self._ns,
                client.V1Secret(
                    metadata=client.V1ObjectMeta(name=sname, namespace=self._ns),
                    string_data={key: value},
                ),
            )

    def list_user_envs(self, mm_user_id: str) -> list[str]:
        sname = env_secret_name(self._secret, mm_user_id)
        try:
            secret = self._core.read_namespaced_secret(sname, self._ns)
            return sorted((secret.data or {}).keys())
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return []
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENV_LIST).inc()
            raise

    def delete_user_env(self, mm_user_id: str, key: str) -> bool:
        sname = env_secret_name(self._secret, mm_user_id)
        try:
            secret = self._core.read_namespaced_secret(sname, self._ns)
            if key not in (secret.data or {}):
                return False
            self._core.patch_namespaced_secret(sname, self._ns, {"data": {key: None}})
            return True
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_ENV_DELETE).inc()
            raise

    def get_workspace_file(self, mm_user_id: str, filename: str) -> str | None:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
            content = (cm.data or {}).get(filename)
            if content is None or content == _workspace_default(filename):
                return None
            return content
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_WORKSPACE_FILE_GET).inc()
            raise

    def set_workspace_file(self, mm_user_id: str, filename: str, content: str) -> None:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            self._core.patch_namespaced_config_map(name, self._ns, {"data": {filename: content}})
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                metrics.k8s_errors_total.labels(op=metrics.K8S_OP_WORKSPACE_FILE_SET).inc()
                raise
            self._core.create_namespaced_config_map(
                self._ns,
                client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=name, namespace=self._ns),
                    data={**_workspace_default_data(), filename: content},
                ),
            )

    def reset_workspace_file(self, mm_user_id: str, filename: str) -> bool:
        name = identity_configmap_name(self._secret, mm_user_id)
        try:
            cm = self._core.read_namespaced_config_map(name, self._ns)
            default = _workspace_default(filename)
            data = cm.data or {}
            if filename not in data:
                return False
            if data.get(filename) == default:
                return False
            self._core.patch_namespaced_config_map(name, self._ns, {"data": {filename: default}})
            return True
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_WORKSPACE_FILE_RESET).inc()
            raise

    def get_user_token(self, mm_user_id: str) -> str | None:
        sname = env_secret_name(self._secret, mm_user_id)
        try:
            secret = self._core.read_namespaced_secret(sname, self._ns)
            data = secret.data
            if not isinstance(data, dict):
                return None
            raw = data.get(TOKEN_KEY)
            if not isinstance(raw, str):
                return None
            return base64.b64decode(raw).decode()
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            metrics.k8s_errors_total.labels(op=metrics.K8S_OP_TOKEN_GET).inc()
            raise

    def set_user_token(self, mm_user_id: str, token: str) -> None:
        self.set_user_env(mm_user_id, TOKEN_KEY, token)

    def reset_user_token(self, mm_user_id: str) -> bool:
        return self.delete_user_env(mm_user_id, TOKEN_KEY)
