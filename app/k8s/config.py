import tomli_w

from app.config import Settings


class ZeroClawConfigBuilder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(
        self,
        env_keys: list[str],
        model: str | None = None,
        autonomy: str = "full",
        api_key_override: str | None = None,
    ) -> str:
        s = self._settings
        doc: dict = {
            "schema_version": 3,
            "onboard_state": {
                "quickstart_completed": True,
                "completed_sections": [
                    "model_provider",
                    "risk_profile",
                    "memory",
                    "channels",
                    "peer_groups",
                    "identity",
                ],
            },
            "agents": {
                "default": {
                    "model_provider": "openai.default",
                    "risk_profile": "default",
                    "runtime_profile": "default",
                }
            },
            "gateway": {"allow_public_bind": True, "require_pairing": False},
            "observability": {
                "backend": "log",
                "log_persistence": "full",
                "log_persistence_path": "/zeroclaw-data/workspace/state/runtime-trace.jsonl",
                "log_tool_io": "full",
            },
            "scheduler": {"enabled": False},
            "browser": {"enabled": False},
            "cron": {"enabled": False, "catch_up_on_startup": False},
            "risk_profiles": {
                "default": {
                    "level": autonomy,
                    "allowed_commands": s.allowed_commands,
                    "shell_env_passthrough": env_keys,
                }
            },
            "runtime_profiles": {
                "default": {
                    "max_tool_iterations": 150,
                    "max_actions_per_hour": 100000,
                }
            },
            "http_request": {"allow_private_hosts": True},
            "web_fetch": {
                "allowed_domain": ["*"],
                "allowed_private_hosts": [
                    "192.168.100.231",
                    "192.168.0.0/16",
                    "172.16.0.0/12",
                    "10.0.0.0/8",
                ],
            },
            "providers": {
                "models": {
                    "openai": {
                        "default": {
                            "api_key": api_key_override
                            if api_key_override is not None
                            else s.openai_api_key,
                            "uri": s.openai_base_url,
                            "model": model if model is not None else s.default_model,
                        }
                    }
                }
            },
        }
        return tomli_w.dumps(doc)
