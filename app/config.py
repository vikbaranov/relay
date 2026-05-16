import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mattermost_url: str
    mattermost_port: int = 443
    mattermost_team: str
    mattermost_bot_token: str
    mattermost_bot_username: str

    k8s_namespace: str = "sandbox"
    k8s_mode: str = "incluster"  # incluster | kubeconfig
    k8s_kubeconfig_path: str | None = None
    k8s_name_secret: str  # HMAC secret → K8s object names

    zeroclaw_image: str = "ghcr.io/zeroclaw-labs/zeroclaw:latest"
    zeroclaw_port: int = 42617
    zeroclaw_data_path: str = "/zeroclaw-data/workspace"
    zeroclaw_configmap: str = "zeroclaw-config"
    zeroclaw_config_key: str = "config.toml"
    zeroclaw_config_mount: str = "/zeroclaw-data/.zeroclaw/"
    zeroclaw_cpu_request: str = "500m"
    zeroclaw_cpu_limit: str = "2"
    zeroclaw_memory_request: str = "1Gi"
    zeroclaw_memory_limit: str = "4Gi"

    k8s_label_part_of: str = "ai.ops-agent.io/part-of"
    k8s_annotation_mm_user: str = "ai.ops-agent.io/mm-user-id"
    k8s_annotation_last_activity: str = "ai.ops-agent.io/last-activity"
    k8s_part_of_value: str = "zeroclaw-runtime"

    openai_api_key: str
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    user_pvc_size: str = "5Gi"
    user_pvc_storage_class: str | None = None

    webhook_host_port: int = 8579
    webhook_public_url: str = ""

    idle_timeout_seconds: int = 3600
    pod_ready_timeout_seconds: int = 120
    reaper_interval_seconds: int = 60

    log_level: int = logging.INFO

    ssl_verify: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
