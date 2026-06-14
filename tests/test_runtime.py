"""LifecycleManager and UserStateManager unit tests with mocked K8s clients."""

import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from kubernetes import client as k8s_client
from pydantic import ValidationError

from app.config import Settings
from app.identity import identity_configmap_name, object_name, zeroclaw_config_secret_name
from app.k8s.config import ZeroClawConfigBuilder
from app.k8s.controller import RuntimeController
from app.k8s.lifecycle import LifecycleManager
from app.k8s.provisioner import ResourceProvisioner
from app.k8s.user_state import UserStateManager
from app.k8s.workspace import _workspace_default


def _settings(**overrides) -> Settings:
    base = dict(
        mattermost_url="http://mm",
        mattermost_team="t",
        mattermost_bot_token="tok",
        mattermost_bot_username="bot",
        k8s_name_secret="test-secret",
        k8s_mode="kubeconfig",
        allowed_models="gpt-4o-mini,gpt-4o",
    )
    base.update(overrides)
    return Settings(**base)


def _make_controller_and_state(settings=None):
    s = settings or _settings()
    core = MagicMock()
    apps = MagicMock()
    secret = s.k8s_name_secret.encode()
    ns = s.k8s_namespace
    config_builder = ZeroClawConfigBuilder(s)
    provisioner = ResourceProvisioner(
        settings=s, core=core, apps=apps, secret=secret, ns=ns, config_builder=config_builder
    )
    user_state = UserStateManager(
        core=core,
        apps=apps,
        secret=secret,
        ns=ns,
        restart_fn=lambda mm_user_id, **kw: None,
        allowed_models=s.allowed_models,
    )
    controller = RuntimeController(
        settings=s,
        core=core,
        apps=apps,
        secret=secret,
        ns=ns,
        provisioner=provisioner,
        user_state=user_state,
        config_builder=config_builder,
    )
    return controller, provisioner, user_state, core, apps


def _make_lifecycle_and_state(settings=None):
    s = settings or _settings()
    core = MagicMock()
    apps = MagicMock()
    secret = s.k8s_name_secret.encode()
    ns = s.k8s_namespace
    lifecycle = LifecycleManager(settings=s, core=core, apps=apps, secret=secret, ns=ns)
    user_state = UserStateManager(
        core=core,
        apps=apps,
        secret=secret,
        ns=ns,
        restart_fn=lifecycle.restart_if_running,
        allowed_models=s.allowed_models,
    )
    lifecycle.set_user_state(user_state)
    return lifecycle, user_state, core, apps


class TestSettingsModels:
    def test_allowed_models_are_parsed_from_comma_separated_string(self):
        settings = _settings(allowed_models="gpt-4o-mini, gpt-4o ,gpt-4.1")

        assert settings.allowed_models == ["gpt-4o-mini", "gpt-4o", "gpt-4.1"]
        assert settings.default_model == "gpt-4o-mini"

    def test_allowed_models_are_parsed_from_environment(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_URL", "http://mm")
        monkeypatch.setenv("MATTERMOST_TEAM", "t")
        monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "tok")
        monkeypatch.setenv("MATTERMOST_BOT_USERNAME", "bot")
        monkeypatch.setenv("K8S_NAME_SECRET", "test-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("ALLOWED_MODELS", "gpt-4o-mini,gpt-4o")

        settings = Settings()

        assert settings.allowed_models == ["gpt-4o-mini", "gpt-4o"]
        assert settings.default_model == "gpt-4o-mini"

    def test_allowed_commands_are_parsed_from_environment(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_URL", "http://mm")
        monkeypatch.setenv("MATTERMOST_TEAM", "t")
        monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "tok")
        monkeypatch.setenv("MATTERMOST_BOT_USERNAME", "bot")
        monkeypatch.setenv("K8S_NAME_SECRET", "test-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("ALLOWED_MODELS", "gpt-4o-mini,gpt-4o")
        monkeypatch.setenv("ALLOWED_COMMANDS", "git, ls ,curl")

        settings = Settings()

        assert settings.allowed_commands == ["git", "ls", "curl"]

    def test_allowed_models_rejects_empty_string(self):
        with pytest.raises(ValidationError):
            _settings(allowed_models="")

    def test_allowed_models_rejects_only_commas_and_spaces(self):
        with pytest.raises(ValidationError):
            _settings(allowed_models=" , , ")


class TestEnsureRuntime:
    def test_creates_resources_on_first_call(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        core.read_namespaced_persistent_volume_claim.side_effect = (
            k8s_client.exceptions.ApiException(status=404)
        )
        core.read_namespaced_service.side_effect = k8s_client.exceptions.ApiException(status=404)
        apps.read_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(status=404)

        dns = lifecycle.ensure_all("user1")

        assert dns.startswith("zc-")
        assert dns.endswith(".svc.cluster.local")
        core.create_namespaced_persistent_volume_claim.assert_called_once()
        core.create_namespaced_service.assert_called_once()
        apps.create_namespaced_deployment.assert_called_once()

    def test_created_runtime_resources_are_labeled_with_mm_user_id(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        core.read_namespaced_persistent_volume_claim.side_effect = (
            k8s_client.exceptions.ApiException(status=404)
        )
        core.read_namespaced_service.side_effect = k8s_client.exceptions.ApiException(status=404)
        apps.read_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(status=404)

        lifecycle.ensure_all("user1")

        pvc_body = core.create_namespaced_persistent_volume_claim.call_args[0][1]
        service_body = core.create_namespaced_service.call_args[0][1]
        deploy_body = apps.create_namespaced_deployment.call_args[0][1]

        assert pvc_body.metadata.labels["ai.relay.io/mm-user-id"] == "user1"
        assert service_body.metadata.labels["ai.relay.io/mm-user-id"] == "user1"
        assert deploy_body.metadata.labels["ai.relay.io/mm-user-id"] == "user1"
        assert deploy_body.spec.template.metadata.labels["ai.relay.io/mm-user-id"] == "user1"

    def test_created_identity_configmap_is_labeled_with_mm_user_id(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        identity_name = identity_configmap_name(b"test-secret", "user1")
        identity_cm_body = next(
            call[0][1]
            for call in core.create_namespaced_config_map.call_args_list
            if call[0][1].metadata.name == identity_name
        )
        assert identity_cm_body.metadata.labels["ai.relay.io/mm-user-id"] == "user1"

    def test_skips_creation_if_resources_exist(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        core.create_namespaced_persistent_volume_claim.assert_not_called()
        apps.create_namespaced_deployment.assert_not_called()

    def test_scales_up_idle_deployment(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 0
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        patch_body = next(
            call[0][2]
            for call in apps.patch_namespaced_deployment.call_args_list
            if call[0][2].get("spec", {}).get("replicas") == 1
        )
        assert patch_body["spec"]["replicas"] == 1

    def test_patches_existing_deployment_to_secret_config_volume(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        existing_deploy.spec.template.spec.volumes = [
            k8s_client.V1Volume(
                name="model-config",
                config_map=k8s_client.V1ConfigMapVolumeSource(name="zeroclaw-identity-default"),
            )
        ]
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        patch_body = apps.patch_namespaced_deployment.call_args[0][2]
        volumes = patch_body["spec"]["template"]["spec"]["volumes"]
        expected_secret = zeroclaw_config_secret_name(b"test-secret", "user1")
        assert volumes == [
            {
                "name": "model-config",
                "secret": {"secretName": expected_secret},
                "configMap": None,
            }
        ]

    def test_returns_correct_service_dns(self):
        s = _settings(k8s_namespace="sandbox", k8s_name_secret="s")
        core = MagicMock()
        apps = MagicMock()
        config_builder = ZeroClawConfigBuilder(s)
        provisioner = ResourceProvisioner(
            settings=s,
            core=core,
            apps=apps,
            secret=b"s",
            ns="sandbox",
            config_builder=config_builder,
        )
        user_state = UserStateManager(
            core=core,
            apps=apps,
            secret=b"s",
            ns="sandbox",
            restart_fn=lambda mm_user_id, **kw: None,
            allowed_models=s.allowed_models,
        )
        controller = RuntimeController(
            settings=s,
            core=core,
            apps=apps,
            secret=b"s",
            ns="sandbox",
            provisioner=provisioner,
            user_state=user_state,
            config_builder=config_builder,
        )
        existing = MagicMock()
        existing.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing

        dns = controller.ensure_all("user1")
        name = object_name(b"s", "user1")
        assert dns == f"{name}.sandbox.svc.cluster.local"

    def test_shared_configmap_ensured_only_once_per_lifecycle(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")
        first_replace_count = core.replace_namespaced_config_map.call_count

        lifecycle.ensure_all("user1")
        assert core.replace_namespaced_config_map.call_count == first_replace_count

    def test_shared_configmap_does_not_contain_openai_api_key(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(openai_api_key="sk-secret-fixture")
        )
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        cm_body = core.replace_namespaced_config_map.call_args[0][2]
        assert "config.toml" not in cm_body.data
        assert "sk-secret-fixture" not in str(cm_body.data)

    def test_creates_per_user_config_secret_with_provider_settings(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(
                openai_api_key="sk-secret-fixture",
                openai_base_url="custom:https://example.test/v1",
            )
        )
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert "[providers.models.openai.default]" in config_toml
        assert 'uri = "custom:https://example.test/v1"' in config_toml
        assert 'model = "gpt-4o-mini"' in config_toml
        assert 'api_key = "sk-secret-fixture"' in config_toml

    def test_user_config_uses_default_allowed_model(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="default-model,other-model")
        )
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert 'model = "default-model"' in config_toml

    def test_user_config_uses_allowed_model_override(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="default-model,custom-model")
        )
        identity_cm = MagicMock()
        identity_cm.data = {"MODEL": "custom-model"}
        core.read_namespaced_config_map.return_value = identity_cm
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert 'model = "custom-model"' in config_toml

    def test_user_config_ignores_stale_model_override(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="default-model,custom-model")
        )
        identity_cm = MagicMock()
        identity_cm.data = {"MODEL": "removed-model"}
        core.read_namespaced_config_map.return_value = identity_cm
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert 'model = "default-model"' in config_toml

    def test_deployment_mounts_per_user_config_secret(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(openai_api_key="sk-secret-fixture")
        )
        core.read_namespaced_persistent_volume_claim.side_effect = (
            k8s_client.exceptions.ApiException(status=404)
        )
        core.read_namespaced_service.side_effect = k8s_client.exceptions.ApiException(status=404)
        apps.read_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(status=404)

        lifecycle.ensure_all("user1")

        deploy_body = apps.create_namespaced_deployment.call_args[0][1]
        model_config_volume = next(
            v for v in deploy_body.spec.template.spec.volumes if v.name == "model-config"
        )
        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        assert model_config_volume.secret.secret_name == expected_name
        assert model_config_volume.config_map is None

    def test_user_config_shell_env_passthrough_reflects_user_envs(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        env_secret = MagicMock()
        env_secret.data = {"GITHUB_TOKEN": "dG9rZW4=", "MY_KEY": "dmFsdWU="}
        core.read_namespaced_secret.side_effect = [
            k8s_client.exceptions.ApiException(status=404),  # provider credentials → create
            env_secret,  # user env keys read
            env_secret,  # get_user_token read (no TOKEN_KEY → no override)
            k8s_client.exceptions.ApiException(status=404),  # user zc-config → create
        ]
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert "GITHUB_TOKEN" in config_toml
        assert "MY_KEY" in config_toml

    def test_restart_updates_user_config_before_pod_restart(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        identity_cm = MagicMock()
        identity_cm.data = {"MODEL": "gpt-4o"}
        core.read_namespaced_config_map.return_value = identity_cm
        env_secret = MagicMock()
        env_secret.data = {"GITHUB_TOKEN": "dG9rZW4="}
        core.read_namespaced_secret.return_value = env_secret

        lifecycle.restart_if_running("user1")

        cname = zeroclaw_config_secret_name(b"test-secret", "user1")
        patch_call = core.patch_namespaced_secret.call_args
        assert patch_call[0][0] == cname
        config_toml = patch_call[0][2]["stringData"]["config.toml"]
        assert "GITHUB_TOKEN" in config_toml
        assert 'model = "gpt-4o"' in config_toml
        apps.patch_namespaced_deployment.assert_called_once()

    def test_restart_propagates_non_404_config_patch_error_without_pod_restart(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        env_secret = MagicMock()
        env_secret.data = {"GITHUB_TOKEN": "dG9rZW4="}
        core.read_namespaced_secret.return_value = env_secret
        core.patch_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=500)

        with pytest.raises(k8s_client.exceptions.ApiException):
            lifecycle.restart_if_running("user1")

        apps.patch_namespaced_deployment.assert_not_called()

    def test_restart_propagates_non_404_deployment_patch_error(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        env_secret = MagicMock()
        env_secret.data = {"GITHUB_TOKEN": "dG9rZW4="}
        core.read_namespaced_secret.return_value = env_secret
        apps.patch_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(
            status=500
        )

        with pytest.raises(k8s_client.exceptions.ApiException):
            lifecycle.restart_if_running("user1")

    def test_ensure_runtime_calls_ensure_identity_configmap(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        read_calls = []

        def _read_cm(name, ns):
            read_calls.append(name)
            if "identity" in name:
                raise k8s_client.exceptions.ApiException(status=404)
            return MagicMock()

        core.read_namespaced_config_map.side_effect = _read_cm
        lifecycle.ensure_all("user1")
        identity_name = identity_configmap_name(b"test-secret", "user1")
        assert any(identity_name in c for c in read_calls)

    def test_user_config_autonomy_defaults_to_full(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert 'level = "full"' in config_toml

    def test_user_config_uses_supervised_autonomy_override(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        identity_cm = MagicMock()
        identity_cm.data = {"AUTONOMY": "supervised"}
        core.read_namespaced_config_map.return_value = identity_cm
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert 'level = "supervised"' in config_toml

    def test_user_config_ignores_invalid_autonomy_override(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        identity_cm = MagicMock()
        identity_cm.data = {"AUTONOMY": "invalid"}
        core.read_namespaced_config_map.return_value = identity_cm
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        lifecycle.ensure_all("user1")

        expected_name = zeroclaw_config_secret_name(b"test-secret", "user1")
        all_creates = [call[0][1] for call in core.create_namespaced_secret.call_args_list]
        user_config = next(s for s in all_creates if s.metadata.name == expected_name)
        config_toml = user_config.string_data["config.toml"]
        assert 'level = "full"' in config_toml

    def test_restart_updates_user_config_with_autonomy(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        identity_cm = MagicMock()
        identity_cm.data = {"AUTONOMY": "supervised"}
        core.read_namespaced_config_map.return_value = identity_cm
        env_secret = MagicMock()
        env_secret.data = {}
        core.read_namespaced_secret.return_value = env_secret

        lifecycle.restart_if_running("user1")

        patch_call = core.patch_namespaced_secret.call_args
        config_toml = patch_call[0][2]["stringData"]["config.toml"]
        assert 'level = "supervised"' in config_toml

    def test_deployment_mounts_identity_volume(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        core.read_namespaced_persistent_volume_claim.side_effect = (
            k8s_client.exceptions.ApiException(status=404)
        )
        core.read_namespaced_service.side_effect = k8s_client.exceptions.ApiException(status=404)
        apps.read_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(status=404)

        lifecycle.ensure_all("user1")

        deploy_body = apps.create_namespaced_deployment.call_args[0][1]
        volumes = deploy_body.spec.template.spec.volumes
        volume_names = [v.name for v in volumes]
        assert "identity" in volume_names

        mounts = deploy_body.spec.template.spec.containers[0].volume_mounts
        mount_paths = [m.mount_path for m in mounts]
        assert len(mount_paths) == len(set(mount_paths))

        identity_mounts = [m for m in mounts if m.name == "identity"]
        assert len(identity_mounts) == 2
        sub_paths = {m.sub_path for m in identity_mounts}
        assert sub_paths == {"SOUL.md", "IDENTITY.md"}


class TestListIdle:
    def _make_deploy(self, name, last_activity_iso, replicas=1):
        d = MagicMock()
        d.metadata.name = name
        d.metadata.annotations = {"ai.relay.io/last-activity": last_activity_iso}
        d.spec.replicas = replicas
        return d

    def test_returns_idle_deployments(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        fresh_ts = datetime.now(UTC).isoformat()
        apps.list_namespaced_deployment.return_value = MagicMock(
            items=[
                self._make_deploy("zc-old", old_ts),
                self._make_deploy("zc-new", fresh_ts),
            ]
        )
        idle = lifecycle.list_idle(ttl_seconds=3600)
        assert idle == ["zc-old"]

    def test_skips_already_scaled_down(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        apps.list_namespaced_deployment.return_value = MagicMock(
            items=[self._make_deploy("zc-old", old_ts, replicas=0)]
        )
        idle = lifecycle.list_idle(ttl_seconds=60)
        assert idle == []


class TestScaleDown:
    def test_patches_replicas_to_zero(self):
        lifecycle, _, core, apps = _make_lifecycle_and_state()
        lifecycle.scale_down("zc-abc")
        apps.patch_namespaced_deployment.assert_called_once()
        body = apps.patch_namespaced_deployment.call_args[0][2]
        assert body["spec"]["replicas"] == 0


class TestWorkspaceFiles:
    def test_get_returns_none_when_configmap_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        assert user_state.get_workspace_file("user1", "SOUL.md") is None

    def test_get_returns_content_from_configmap(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"SOUL.md": "custom soul"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_workspace_file("user1", "SOUL.md") == "custom soul"

    def test_get_returns_none_for_missing_key(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"IDENTITY.md": "identity"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_workspace_file("user1", "SOUL.md") is None

    def test_set_patches_existing_configmap(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        user_state.set_workspace_file("user1", "SOUL.md", "new soul")
        core.patch_namespaced_config_map.assert_called_once()
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["SOUL.md"] == "new soul"

    def test_set_creates_configmap_when_absent(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        core.patch_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(
            status=404
        )
        user_state.set_workspace_file("user1", "SOUL.md", "new soul")
        core.create_namespaced_config_map.assert_called_once()
        cm_body = core.create_namespaced_config_map.call_args[0][1]
        assert cm_body.data["SOUL.md"] == "new soul"
        assert cm_body.data["IDENTITY.md"] == _workspace_default("IDENTITY.md")

    def test_set_restarts_running_deployment(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        user_state.set_workspace_file("user1", "SOUL.md", "new soul")
        apps.patch_namespaced_deployment.assert_called_once()

    def test_reset_returns_false_when_configmap_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        assert user_state.reset_workspace_file("user1", "SOUL.md") is False

    def test_reset_returns_false_when_key_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.reset_workspace_file("user1", "SOUL.md") is False

    def test_reset_patches_key_to_default_and_restarts(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"SOUL.md": "custom"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.reset_workspace_file("user1", "SOUL.md") is True
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["SOUL.md"] == _workspace_default("SOUL.md")
        apps.patch_namespaced_deployment.assert_called_once()

    def test_get_returns_none_for_default_content(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"SOUL.md": _workspace_default("SOUL.md")}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_workspace_file("user1", "SOUL.md") is None

    def test_ensure_identity_configmap_creates_on_first_call(self):
        _, provisioner, _, core, _ = _make_controller_and_state()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        s = _settings()
        labels = {s.k8s_label_mm_user: "user1"}
        provisioner.ensure_identity_configmap("user1", labels, {})
        core.create_namespaced_config_map.assert_called_once()
        cm_body = core.create_namespaced_config_map.call_args[0][1]
        assert cm_body.metadata.name == identity_configmap_name(b"test-secret", "user1")

    def test_ensure_identity_configmap_skips_if_exists(self):
        _, provisioner, _, core, _ = _make_controller_and_state()
        core.read_namespaced_config_map.return_value = MagicMock()
        s = _settings()
        labels = {s.k8s_label_mm_user: "user1"}
        provisioner.ensure_identity_configmap("user1", labels, {})
        core.create_namespaced_config_map.assert_not_called()

    def test_get_user_model_returns_override_when_allowed(self):
        _, user_state, core, _ = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        cm = MagicMock()
        cm.data = {"MODEL": "gpt-4o"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_user_model("user1") == "gpt-4o"

    def test_get_user_model_returns_default_when_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        cm = MagicMock()
        cm.data = {}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_user_model("user1") == "gpt-4o-mini"

    def test_get_user_model_returns_default_when_stale(self):
        _, user_state, core, _ = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        cm = MagicMock()
        cm.data = {"MODEL": "removed-model"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_user_model("user1") == "gpt-4o-mini"

    def test_set_user_model_rejects_unknown_model(self):
        _, user_state, _, _ = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        assert user_state.set_user_model("user1", "bad-model") is False

    def test_set_user_model_patches_configmap_and_restarts(self):
        _, user_state, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        assert user_state.set_user_model("user1", "gpt-4o") is True
        core.patch_namespaced_config_map.assert_called_once()
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["MODEL"] == "gpt-4o"
        apps.patch_namespaced_deployment.assert_called_once()

    def test_reset_user_model_returns_false_when_absent(self):
        _, user_state, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        cm = MagicMock()
        cm.data = {}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.reset_user_model("user1") is False
        core.patch_namespaced_config_map.assert_not_called()
        apps.patch_namespaced_deployment.assert_not_called()

    def test_reset_user_model_removes_override_and_restarts(self):
        _, user_state, core, apps = _make_lifecycle_and_state(
            _settings(allowed_models="gpt-4o-mini,gpt-4o")
        )
        cm = MagicMock()
        cm.data = {"MODEL": "gpt-4o"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.reset_user_model("user1") is True
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["MODEL"] is None
        apps.patch_namespaced_deployment.assert_called_once()

    def test_get_user_autonomy_returns_full_when_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_user_autonomy("user1") == "full"

    def test_get_user_autonomy_returns_full_when_configmap_missing(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        assert user_state.get_user_autonomy("user1") == "full"

    def test_get_user_autonomy_returns_supervised_when_set(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"AUTONOMY": "supervised"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_user_autonomy("user1") == "supervised"

    def test_get_user_autonomy_returns_full_for_invalid_value(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"AUTONOMY": "invalid"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.get_user_autonomy("user1") == "full"

    def test_set_user_autonomy_rejects_unknown_level(self):
        _, user_state, _, _ = _make_lifecycle_and_state()
        assert user_state.set_user_autonomy("user1", "turbo") is False

    def test_set_user_autonomy_patches_configmap_and_restarts(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        assert user_state.set_user_autonomy("user1", "supervised") is True
        core.patch_namespaced_config_map.assert_called_once()
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["AUTONOMY"] == "supervised"
        apps.patch_namespaced_deployment.assert_called_once()

    def test_set_user_autonomy_creates_configmap_when_absent(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        core.patch_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(
            status=404
        )
        assert user_state.set_user_autonomy("user1", "supervised") is True
        core.create_namespaced_config_map.assert_called_once()
        cm_body = core.create_namespaced_config_map.call_args[0][1]
        assert cm_body.data["AUTONOMY"] == "supervised"

    def test_reset_user_autonomy_returns_false_when_absent(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.reset_user_autonomy("user1") is False
        core.patch_namespaced_config_map.assert_not_called()
        apps.patch_namespaced_deployment.assert_not_called()

    def test_reset_user_autonomy_returns_false_when_configmap_missing(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        assert user_state.reset_user_autonomy("user1") is False

    def test_reset_user_autonomy_removes_override_and_restarts(self):
        _, user_state, core, apps = _make_lifecycle_and_state()
        cm = MagicMock()
        cm.data = {"AUTONOMY": "supervised"}
        core.read_namespaced_config_map.return_value = cm
        assert user_state.reset_user_autonomy("user1") is True
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["AUTONOMY"] is None
        apps.patch_namespaced_deployment.assert_called_once()


class TestUserStateToken:
    def test_get_user_token_returns_none_when_secret_missing(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        core.read_namespaced_secret.side_effect = k8s_client.exceptions.ApiException(status=404)

        assert user_state.get_user_token("user1") is None

    def test_get_user_token_returns_none_when_key_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        secret = MagicMock()
        secret.data = {"OTHER_KEY": base64.b64encode(b"value").decode()}
        core.read_namespaced_secret.return_value = secret

        assert user_state.get_user_token("user1") is None

    def test_get_user_token_decodes_base64_value(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        secret = MagicMock()
        secret.data = {"OPENAI_API_KEY_OVERRIDE": base64.b64encode(b"sk-test-key").decode()}
        core.read_namespaced_secret.return_value = secret

        assert user_state.get_user_token("user1") == "sk-test-key"

    def test_set_user_token_patches_secret_with_string_data(self):
        _, user_state, core, _ = _make_lifecycle_and_state()

        user_state.set_user_token("user1", "sk-my-key")

        first_body = core.patch_namespaced_secret.call_args_list[0][0][2]
        assert first_body == {"stringData": {"OPENAI_API_KEY_OVERRIDE": "sk-my-key"}}

    def test_reset_user_token_returns_false_when_key_absent(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        secret = MagicMock()
        secret.data = {}
        core.read_namespaced_secret.return_value = secret

        assert user_state.reset_user_token("user1") is False

    def test_reset_user_token_deletes_key_and_returns_true(self):
        _, user_state, core, _ = _make_lifecycle_and_state()
        secret = MagicMock()
        secret.data = {"OPENAI_API_KEY_OVERRIDE": base64.b64encode(b"sk-key").decode()}
        core.read_namespaced_secret.return_value = secret

        result = user_state.reset_user_token("user1")

        assert result is True
        # delete_user_env patches data key to None
        patch_calls = [c for c in core.patch_namespaced_secret.call_args_list]
        assert any(c[0][2] == {"data": {"OPENAI_API_KEY_OVERRIDE": None}} for c in patch_calls)


class TestZeroclawConfigTokenOverride:
    def test_config_toml_uses_global_key_when_no_override(self):
        import tomllib

        s = _settings(openai_api_key="sk-global", openai_base_url="https://api.openai.com/v1")
        builder = ZeroClawConfigBuilder(s)
        doc = tomllib.loads(builder.build([], api_key_override=None))
        assert doc["providers"]["models"]["openai"]["default"]["api_key"] == "sk-global"

    def test_config_toml_uses_override_key_when_provided(self):
        import tomllib

        s = _settings(openai_api_key="sk-global", openai_base_url="https://api.openai.com/v1")
        builder = ZeroClawConfigBuilder(s)
        doc = tomllib.loads(builder.build([], api_key_override="sk-user-override"))
        assert doc["providers"]["models"]["openai"]["default"]["api_key"] == "sk-user-override"

    def test_get_user_env_keys_excludes_token_key(self):
        import base64

        _, provisioner, _, core, _ = _make_controller_and_state()
        secret = MagicMock()
        secret.data = {
            "MY_VAR": base64.b64encode(b"val").decode(),
            "OPENAI_API_KEY_OVERRIDE": base64.b64encode(b"sk-key").decode(),
        }
        core.read_namespaced_secret.return_value = secret
        keys = provisioner.get_user_env_keys("user1")
        assert "OPENAI_API_KEY_OVERRIDE" not in keys
        assert "MY_VAR" in keys

    def test_ensure_user_zeroclaw_config_passes_token_override_to_toml(self):
        import tomllib

        _, provisioner, _, core, _ = _make_controller_and_state(
            _settings(openai_api_key="sk-global", openai_base_url="https://api.openai.com/v1")
        )
        core.read_namespaced_secret.return_value = MagicMock()  # secret exists → replace path
        provisioner.ensure_user_config_secret("user1", [], "gpt-4o-mini", "full", "sk-user", {}, {})
        replace_call = core.replace_namespaced_secret.call_args
        body = replace_call[0][2]
        doc = tomllib.loads(body.string_data["config.toml"])
        assert doc["providers"]["models"]["openai"]["default"]["api_key"] == "sk-user"

    def test_restart_if_running_passes_token_override_to_toml(self):
        import base64
        import tomllib

        controller, _, _, core, apps = _make_controller_and_state(
            _settings(openai_api_key="sk-global", openai_base_url="https://api.openai.com/v1")
        )
        env_secret = MagicMock()
        env_secret.data = {"OPENAI_API_KEY_OVERRIDE": base64.b64encode(b"sk-user").decode()}
        core.read_namespaced_secret.return_value = env_secret
        controller.restart_if_running("user1")
        config_toml = core.patch_namespaced_secret.call_args[0][2]["stringData"]["config.toml"]
        doc = tomllib.loads(config_toml)
        assert doc["providers"]["models"]["openai"]["default"]["api_key"] == "sk-user"
