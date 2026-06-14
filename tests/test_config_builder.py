import tomllib

from app.config import Settings
from app.k8s.config import ZeroClawConfigBuilder


def _settings(**kwargs) -> Settings:
    base = dict(
        mattermost_url="http://mm",
        mattermost_team="t",
        mattermost_bot_token="tok",
        mattermost_bot_username="bot",
        k8s_name_secret="s",
        k8s_mode="kubeconfig",
        allowed_models="gpt-4o-mini,gpt-4o",
        openai_api_key="sk-global",
        openai_base_url="https://api.openai.com/v1",
    )
    base.update(kwargs)
    return Settings(**base)


class TestZeroClawConfigBuilder:
    def test_build_returns_valid_toml(self):
        builder = ZeroClawConfigBuilder(_settings())
        doc = tomllib.loads(builder.build([]))
        assert doc["schema_version"] == 3

    def test_build_uses_first_allowed_model_as_default(self):
        builder = ZeroClawConfigBuilder(_settings(allowed_models="gpt-4o-mini,gpt-4o"))
        doc = tomllib.loads(builder.build([]))
        assert doc["providers"]["models"]["openai"]["default"]["model"] == "gpt-4o-mini"

    def test_build_uses_provided_model(self):
        builder = ZeroClawConfigBuilder(_settings(allowed_models="gpt-4o-mini,gpt-4o"))
        doc = tomllib.loads(builder.build([], model="gpt-4o"))
        assert doc["providers"]["models"]["openai"]["default"]["model"] == "gpt-4o"

    def test_build_uses_global_api_key_when_no_override(self):
        builder = ZeroClawConfigBuilder(_settings(openai_api_key="sk-global"))
        doc = tomllib.loads(builder.build([]))
        assert doc["providers"]["models"]["openai"]["default"]["api_key"] == "sk-global"

    def test_build_uses_override_api_key_when_provided(self):
        builder = ZeroClawConfigBuilder(_settings(openai_api_key="sk-global"))
        doc = tomllib.loads(builder.build([], api_key_override="sk-user"))
        assert doc["providers"]["models"]["openai"]["default"]["api_key"] == "sk-user"

    def test_build_includes_env_keys_in_shell_passthrough(self):
        builder = ZeroClawConfigBuilder(_settings())
        doc = tomllib.loads(builder.build(["GITHUB_TOKEN", "MY_VAR"]))
        passthrough = doc["risk_profiles"]["default"]["shell_env_passthrough"]
        assert "GITHUB_TOKEN" in passthrough
        assert "MY_VAR" in passthrough

    def test_build_sets_autonomy_level(self):
        builder = ZeroClawConfigBuilder(_settings())
        doc = tomllib.loads(builder.build([], autonomy="supervised"))
        assert doc["risk_profiles"]["default"]["level"] == "supervised"

    def test_build_sets_openai_uri(self):
        builder = ZeroClawConfigBuilder(_settings(openai_base_url="https://custom.example.com/v1"))
        doc = tomllib.loads(builder.build([]))
        assert (
            doc["providers"]["models"]["openai"]["default"]["uri"]
            == "https://custom.example.com/v1"
        )
