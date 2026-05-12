from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mattermost_url: str
    mattermost_port: int = 443
    mattermost_team: str
    mattermost_bot_token: str
    mattermost_bot_username: str

    k8s_namespace: str = "ai-assistants"
    k8s_mode: str = "incluster"  # incluster | kubeconfig
    k8s_kubeconfig_path: str | None = None
    k8s_name_secret: str  # HMAC secret → K8s object names

    zeroclaw_image: str = "ghcr.io/zeroclaw-labs/zeroclaw:latest"
    zeroclaw_port: int = 42617
    zeroclaw_data_path: str = "/zeroclaw-data"
    zeroclaw_cpu_request: str = "500m"
    zeroclaw_cpu_limit: str = "2"
    zeroclaw_memory_request: str = "1Gi"
    zeroclaw_memory_limit: str = "4Gi"

    user_pvc_size: str = "5Gi"
    user_pvc_storage_class: str | None = None

    idle_timeout_seconds: int = 3600
    pod_ready_timeout_seconds: int = 120
    reaper_interval_seconds: int = 60

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
