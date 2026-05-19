"""RuntimeManager unit tests with mocked K8s clients."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from kubernetes import client as k8s_client

from app.config import Settings
from app.identity import object_name
from app.k8s.runtime import ANNOTATION_LAST_ACTIVITY, RuntimeManager


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
