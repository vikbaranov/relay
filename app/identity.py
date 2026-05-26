import hashlib
import hmac


def _runtime_key(secret: bytes, mm_user_id: str) -> str:
    return hmac.new(secret, mm_user_id.encode(), hashlib.sha256).hexdigest()[:20]


def object_name(secret: bytes, mm_user_id: str) -> str:
    """DNS-safe K8s name for Deployment, Service, and base of PVC."""
    return f"zc-{_runtime_key(secret, mm_user_id)}"


def pvc_name(secret: bytes, mm_user_id: str) -> str:
    return f"{object_name(secret, mm_user_id)}-data"


def env_secret_name(secret: bytes, mm_user_id: str) -> str:
    return f"{object_name(secret, mm_user_id)}-env"


def identity_configmap_name(secret: bytes, mm_user_id: str) -> str:
    return f"{object_name(secret, mm_user_id)}-identity"


def zeroclaw_config_secret_name(secret: bytes, mm_user_id: str) -> str:
    return f"{object_name(secret, mm_user_id)}-config"


def session_id(scope: str, generation: int = 0) -> str:
    """ZeroClaw WS session id for a Mattermost conversation scope."""
    digest = hashlib.sha256(f"{scope}:{generation}".encode()).hexdigest()[:24]
    return f"mm-{digest}"
