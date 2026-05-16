import hashlib
import hmac


def _runtime_key(secret: bytes, mm_user_id: str) -> str:
    return hmac.new(secret, mm_user_id.encode(), hashlib.sha256).hexdigest()[:20]


def object_name(secret: bytes, mm_user_id: str) -> str:
    """DNS-safe K8s name for Deployment, Service, and base of PVC."""
    return f"zc-{_runtime_key(secret, mm_user_id)}"


def pvc_name(secret: bytes, mm_user_id: str) -> str:
    return f"{object_name(secret, mm_user_id)}-data"


def session_id(mm_user_id: str, thread_id: str) -> str:
    """ZeroClaw WS session id scoped to a Mattermost thread."""
    return f"mm-{mm_user_id}-{thread_id}"
