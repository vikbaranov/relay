import base64
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client

from app.k8s.skills import PodNotRunningError, SkillError, SkillManager

_SECRET = b"test-secret"
_NS = "test-ns"


def _make_manager():
    core = MagicMock(spec=client.CoreV1Api)
    return SkillManager(core=core, secret=_SECRET, ns=_NS), core


def _set_running_pod(core, pod_name="pod-abc"):
    pod = MagicMock()
    pod.metadata.name = pod_name
    pod.status.phase = "Running"
    pod_list = MagicMock()
    pod_list.items = [pod]
    core.list_namespaced_pod.return_value = pod_list


def _set_no_pod(core):
    pod_list = MagicMock()
    pod_list.items = []
    core.list_namespaced_pod.return_value = pod_list


def _mock_ws(stdout="", returncode=0):
    ws = MagicMock()
    ws.is_open.side_effect = [True, False]
    ws.peek_stdout.return_value = bool(stdout)
    ws.read_stdout.return_value = stdout
    ws.peek_stderr.return_value = False
    ws.returncode = returncode
    return ws


class TestListSkills:
    @patch("app.k8s.skills.stream")
    def test_returns_raw_output(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(stdout="git-assistant v0.2.0\n")
        result = manager.list_skills("user1")
        assert result == "git-assistant v0.2.0"

    @patch("app.k8s.skills.stream")
    def test_empty_returns_empty_string(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(stdout="")
        result = manager.list_skills("user1")
        assert result == ""

    def test_pod_not_running_raises(self):
        manager, core = _make_manager()
        _set_no_pod(core)
        with pytest.raises(PodNotRunningError):
            manager.list_skills("user1")

    @patch("app.k8s.skills.stream")
    @patch("app.k8s.skills.time")
    def test_exec_timeout_raises_skill_error(self, mock_time, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        ws = MagicMock()
        ws.is_open.return_value = True  # never exits naturally
        ws.peek_stdout.return_value = False
        ws.peek_stderr.return_value = False
        mock_stream.return_value = ws
        # monotonic: first call sets deadline (t=0), second call in loop exceeds it (t=999)
        mock_time.monotonic.side_effect = [0, 999]
        with pytest.raises(SkillError, match="timed out"):
            manager.list_skills("user1")
        ws.close.assert_called()


class TestCreateSkill:
    @patch("app.k8s.skills.stream")
    def test_success_runs_zeroclaw_install(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=0)
        manager.create_skill("user1", "my-skill", "# My Skill\nDoes stuff.")
        cmd_str = " ".join(mock_stream.call_args[1]["command"])
        assert "my-skill" in cmd_str
        assert "zeroclaw" in cmd_str

    @patch("app.k8s.skills.stream")
    def test_content_is_base64_encoded(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=0)
        content = "# Skill\nSome content with 'quotes' and \"double\" quotes."
        manager.create_skill("user1", "test-skill", content)
        cmd_str = " ".join(mock_stream.call_args[1]["command"])
        b64 = base64.b64encode(content.encode()).decode()
        assert b64 in cmd_str

    def test_invalid_name_raises_before_exec(self):
        manager, core = _make_manager()
        with pytest.raises(SkillError):
            manager.create_skill("user1", "../evil", "content")
        core.list_namespaced_pod.assert_not_called()

    def test_invalid_name_with_slash_raises(self):
        manager, core = _make_manager()
        with pytest.raises(SkillError):
            manager.create_skill("user1", "a/b", "content")
        core.list_namespaced_pod.assert_not_called()

    @patch("app.k8s.skills.stream")
    def test_exec_failure_raises(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=1)
        with pytest.raises(SkillError):
            manager.create_skill("user1", "my-skill", "# content")

    def test_pod_not_running_raises(self):
        manager, core = _make_manager()
        _set_no_pod(core)
        with pytest.raises(PodNotRunningError):
            manager.create_skill("user1", "my-skill", "# content")


class TestShowSkill:
    @patch("app.k8s.skills.stream")
    def test_returns_content_when_found(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(stdout="# My Skill\nDoes stuff.\n")
        result = manager.show_skill("user1", "my-skill")
        assert result == "# My Skill\nDoes stuff.\n"

    @patch("app.k8s.skills.stream")
    def test_returns_none_when_not_found(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=1)
        assert manager.show_skill("user1", "missing-skill") is None

    @patch("app.k8s.skills.stream")
    def test_exec_uses_cat_on_skill_path(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(stdout="content")
        manager.show_skill("user1", "my-skill")
        cmd = mock_stream.call_args[1]["command"]
        assert cmd == ["cat", "/zeroclaw-data/workspace/skills/my-skill/SKILL.md"]

    def test_invalid_name_raises_before_exec(self):
        manager, core = _make_manager()
        with pytest.raises(SkillError):
            manager.show_skill("user1", "../evil")
        core.list_namespaced_pod.assert_not_called()

    def test_pod_not_running_raises(self):
        manager, core = _make_manager()
        _set_no_pod(core)
        with pytest.raises(PodNotRunningError):
            manager.show_skill("user1", "my-skill")


class TestRemoveSkill:
    @patch("app.k8s.skills.stream")
    def test_returns_true_when_found(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=0)
        assert manager.remove_skill("user1", "my-skill") is True

    @patch("app.k8s.skills.stream")
    def test_returns_false_when_not_found(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=1)
        assert manager.remove_skill("user1", "ghost-skill") is False

    @patch("app.k8s.skills.stream")
    def test_exec_uses_zeroclaw_remove(self, mock_stream):
        manager, core = _make_manager()
        _set_running_pod(core)
        mock_stream.return_value = _mock_ws(returncode=0)
        manager.remove_skill("user1", "my-skill")
        cmd = mock_stream.call_args[1]["command"]
        assert cmd == ["zeroclaw", "skills", "remove", "my-skill"]

    def test_invalid_name_raises_before_exec(self):
        manager, core = _make_manager()
        with pytest.raises(SkillError):
            manager.remove_skill("user1", "../evil")
        core.list_namespaced_pod.assert_not_called()

    def test_pod_not_running_raises(self):
        manager, core = _make_manager()
        _set_no_pod(core)
        with pytest.raises(PodNotRunningError):
            manager.remove_skill("user1", "my-skill")
