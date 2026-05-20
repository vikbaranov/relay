"""RuntimeManager unit tests with mocked K8s clients."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from kubernetes import client as k8s_client

from app.config import Settings
from app.identity import identity_configmap_name, object_name
from app.k8s.runtime import ANNOTATION_LAST_ACTIVITY, RuntimeManager, _workspace_default


def _settings(**overrides) -> Settings:
    base = dict(
        mattermost_url="http://mm",
        mattermost_team="t",
        mattermost_bot_token="tok",
        mattermost_bot_username="bot",
        k8s_name_secret="test-secret",
        k8s_mode="kubeconfig",
    )
    base.update(overrides)
    return Settings(**base)


def _make_runtime(settings=None):
    s = settings or _settings()
    core = MagicMock()
    apps = MagicMock()
    return RuntimeManager(settings=s, core=core, apps=apps), core, apps


class TestEnsureRuntime:
    def test_creates_resources_on_first_call(self):
        rm, core, apps = _make_runtime()
        core.read_namespaced_persistent_volume_claim.side_effect = (
            k8s_client.exceptions.ApiException(status=404)
        )
        core.read_namespaced_service.side_effect = k8s_client.exceptions.ApiException(status=404)
        apps.read_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(status=404)

        dns = rm.ensure_runtime("user1")

        assert dns.startswith("zc-")
        assert dns.endswith(".svc.cluster.local")
        core.create_namespaced_persistent_volume_claim.assert_called_once()
        core.create_namespaced_service.assert_called_once()
        apps.create_namespaced_deployment.assert_called_once()

    def test_skips_creation_if_resources_exist(self):
        rm, core, apps = _make_runtime()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        rm.ensure_runtime("user1")

        core.create_namespaced_persistent_volume_claim.assert_not_called()
        apps.create_namespaced_deployment.assert_not_called()

    def test_scales_up_idle_deployment(self):
        rm, core, apps = _make_runtime()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 0
        apps.read_namespaced_deployment.return_value = existing_deploy

        rm.ensure_runtime("user1")

        apps.patch_namespaced_deployment.assert_called_once()
        patch_body = apps.patch_namespaced_deployment.call_args[0][2]
        assert patch_body["spec"]["replicas"] == 1

    def test_returns_correct_service_dns(self):
        rm, core, apps = _make_runtime()
        existing = MagicMock()
        existing.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing

        s = _settings(k8s_namespace="sandbox", k8s_name_secret="s")
        rm2 = RuntimeManager(settings=s, core=core, apps=apps)
        dns = rm2.ensure_runtime("user1")
        name = object_name(b"s", "user1")
        assert dns == f"{name}.sandbox.svc.cluster.local"

    def test_recreates_shared_configmap_if_deleted_after_first_ensure(self):
        rm, core, apps = _make_runtime()
        existing_deploy = MagicMock()
        existing_deploy.spec.replicas = 1
        apps.read_namespaced_deployment.return_value = existing_deploy

        rm.ensure_runtime("user1")
        core.create_namespaced_config_map.reset_mock()

        def _read_cm(name, ns):
            if name == "zeroclaw-config":
                raise k8s_client.exceptions.ApiException(status=404)
            return MagicMock()

        core.read_namespaced_config_map.side_effect = _read_cm
        rm.ensure_runtime("user1")

        core.create_namespaced_config_map.assert_called_once()
        cm_body = core.create_namespaced_config_map.call_args[0][1]
        assert cm_body.metadata.name == "zeroclaw-config"


class TestListIdle:
    def _make_deploy(self, name, last_activity_iso, replicas=1):
        d = MagicMock()
        d.metadata.name = name
        d.metadata.annotations = {ANNOTATION_LAST_ACTIVITY: last_activity_iso}
        d.spec.replicas = replicas
        return d

    def test_returns_idle_deployments(self):
        rm, core, apps = _make_runtime()
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        fresh_ts = datetime.now(UTC).isoformat()
        apps.list_namespaced_deployment.return_value = MagicMock(
            items=[
                self._make_deploy("zc-old", old_ts),
                self._make_deploy("zc-new", fresh_ts),
            ]
        )
        idle = rm.list_idle(ttl_seconds=3600)
        assert idle == ["zc-old"]

    def test_skips_already_scaled_down(self):
        rm, core, apps = _make_runtime()
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        apps.list_namespaced_deployment.return_value = MagicMock(
            items=[self._make_deploy("zc-old", old_ts, replicas=0)]
        )
        idle = rm.list_idle(ttl_seconds=60)
        assert idle == []


class TestScaleDown:
    def test_patches_replicas_to_zero(self):
        rm, core, apps = _make_runtime()
        rm.scale_down("zc-abc")
        apps.patch_namespaced_deployment.assert_called_once()
        body = apps.patch_namespaced_deployment.call_args[0][2]
        assert body["spec"]["replicas"] == 0


class TestWorkspaceFiles:
    def test_get_returns_none_when_configmap_absent(self):
        rm, core, _ = _make_runtime()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        assert rm.get_workspace_file("user1", "SOUL.md") is None

    def test_get_returns_content_from_configmap(self):
        rm, core, _ = _make_runtime()
        cm = MagicMock()
        cm.data = {"SOUL.md": "custom soul"}
        core.read_namespaced_config_map.return_value = cm
        assert rm.get_workspace_file("user1", "SOUL.md") == "custom soul"

    def test_get_returns_none_for_missing_key(self):
        rm, core, _ = _make_runtime()
        cm = MagicMock()
        cm.data = {"IDENTITY.md": "identity"}
        core.read_namespaced_config_map.return_value = cm
        assert rm.get_workspace_file("user1", "SOUL.md") is None

    def test_set_patches_existing_configmap(self):
        rm, core, apps = _make_runtime()
        rm.set_workspace_file("user1", "SOUL.md", "new soul")
        core.patch_namespaced_config_map.assert_called_once()
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["SOUL.md"] == "new soul"

    def test_set_creates_configmap_when_absent(self):
        rm, core, apps = _make_runtime()
        core.patch_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        rm.set_workspace_file("user1", "SOUL.md", "new soul")
        core.create_namespaced_config_map.assert_called_once()
        cm_body = core.create_namespaced_config_map.call_args[0][1]
        assert cm_body.data["SOUL.md"] == "new soul"
        assert cm_body.data["IDENTITY.md"] == _workspace_default("IDENTITY.md")

    def test_set_restarts_running_deployment(self):
        rm, core, apps = _make_runtime()
        rm.set_workspace_file("user1", "SOUL.md", "new soul")
        apps.patch_namespaced_deployment.assert_called_once()

    def test_reset_returns_false_when_configmap_absent(self):
        rm, core, _ = _make_runtime()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        assert rm.reset_workspace_file("user1", "SOUL.md") is False

    def test_reset_returns_false_when_key_absent(self):
        rm, core, _ = _make_runtime()
        cm = MagicMock()
        cm.data = {}
        core.read_namespaced_config_map.return_value = cm
        assert rm.reset_workspace_file("user1", "SOUL.md") is False

    def test_reset_patches_key_to_default_and_restarts(self):
        rm, core, apps = _make_runtime()
        cm = MagicMock()
        cm.data = {"SOUL.md": "custom"}
        core.read_namespaced_config_map.return_value = cm
        assert rm.reset_workspace_file("user1", "SOUL.md") is True
        _, _, body = core.patch_namespaced_config_map.call_args[0]
        assert body["data"]["SOUL.md"] == _workspace_default("SOUL.md")
        apps.patch_namespaced_deployment.assert_called_once()

    def test_get_returns_none_for_default_content(self):
        rm, core, _ = _make_runtime()
        cm = MagicMock()
        cm.data = {"SOUL.md": _workspace_default("SOUL.md")}
        core.read_namespaced_config_map.return_value = cm
        assert rm.get_workspace_file("user1", "SOUL.md") is None

    def test_ensure_identity_configmap_creates_on_first_call(self):
        rm, core, _ = _make_runtime()
        core.read_namespaced_config_map.side_effect = k8s_client.exceptions.ApiException(status=404)
        rm._ensure_identity_configmap("user1")
        core.create_namespaced_config_map.assert_called_once()
        s = _settings()
        cm_body = core.create_namespaced_config_map.call_args[0][1]
        assert cm_body.metadata.name == identity_configmap_name(b"test-secret", "user1")

    def test_ensure_identity_configmap_skips_if_exists(self):
        rm, core, _ = _make_runtime()
        core.read_namespaced_config_map.return_value = MagicMock()
        rm._ensure_identity_configmap("user1")
        core.create_namespaced_config_map.assert_not_called()

    def test_ensure_runtime_calls_ensure_identity_configmap(self):
        rm, core, apps = _make_runtime()
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
        rm.ensure_runtime("user1")
        identity_name = identity_configmap_name(b"test-secret", "user1")
        assert any(identity_name in c for c in read_calls)

    def test_deployment_mounts_identity_volume(self):
        rm, core, apps = _make_runtime()
        core.read_namespaced_persistent_volume_claim.side_effect = (
            k8s_client.exceptions.ApiException(status=404)
        )
        core.read_namespaced_service.side_effect = k8s_client.exceptions.ApiException(status=404)
        apps.read_namespaced_deployment.side_effect = k8s_client.exceptions.ApiException(status=404)

        rm.ensure_runtime("user1")

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
