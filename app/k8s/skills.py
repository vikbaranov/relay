import base64
import logging
import re
import time

from kubernetes import client
from kubernetes.stream import stream

from app.identity import object_name

logger = logging.getLogger(__name__)

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SkillError(Exception):
    pass


class PodNotRunningError(SkillError):
    pass


class SkillManager:
    def __init__(
        self,
        core: client.CoreV1Api,
        secret: bytes,
        ns: str,
        workspace_path: str = "/zeroclaw-data/workspace",
    ) -> None:
        self._core = core
        self._secret = secret
        self._ns = ns
        self._workspace_path = workspace_path
        self._tmp_dir = f"{workspace_path}/.tmp"

    def list_skills(self, mm_user_id: str) -> str:
        pod_name = self._find_pod(mm_user_id)
        stdout, _ = self._exec(pod_name, ["zeroclaw", "skills", "list"])
        return stdout.strip()

    def create_skill(self, mm_user_id: str, name: str, content: str) -> None:
        if not SKILL_NAME_RE.match(name):
            raise SkillError(f"Invalid skill name: {name!r}")
        pod_name = self._find_pod(mm_user_id)
        b64 = base64.b64encode(content.encode()).decode()
        tmp_path = f"{self._tmp_dir}/{name}"
        cmd = (
            f"mkdir -p {tmp_path} && "
            f"printf '%s' '{b64}' | base64 -d > {tmp_path}/SKILL.md && "
            f"zeroclaw skills install {tmp_path}/ ; "
            f"RC=$? ; rm -rf {tmp_path} ; exit $RC"
        )
        _, rc = self._exec(pod_name, ["sh", "-c", cmd])
        if rc != 0:
            raise SkillError(f"Failed to create skill {name!r}: exit {rc}")

    def show_skill(self, mm_user_id: str, name: str) -> str | None:
        if not SKILL_NAME_RE.match(name):
            raise SkillError(f"Invalid skill name: {name!r}")
        pod_name = self._find_pod(mm_user_id)
        skill_path = f"{self._workspace_path}/skills/{name}/SKILL.md"
        stdout, rc = self._exec(pod_name, ["cat", skill_path])
        if rc != 0:
            return None
        return stdout

    def remove_skill(self, mm_user_id: str, name: str) -> bool:
        if not SKILL_NAME_RE.match(name):
            raise SkillError(f"Invalid skill name: {name!r}")
        pod_name = self._find_pod(mm_user_id)
        _, rc = self._exec(pod_name, ["zeroclaw", "skills", "remove", name])
        return rc == 0

    def _find_pod(self, mm_user_id: str) -> str:
        label = f"app={object_name(self._secret, mm_user_id)}"
        pod_list = self._core.list_namespaced_pod(self._ns, label_selector=label)
        for pod in pod_list.items:
            if pod.status.phase == "Running":
                return pod.metadata.name
        raise PodNotRunningError(f"No running pod for user {mm_user_id!r}")

    def _exec(self, pod_name: str, command: list[str], timeout: int = 60) -> tuple[str, int]:
        resp = stream(
            self._core.connect_get_namespaced_pod_exec,
            pod_name,
            self._ns,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        stdout_buf = ""
        stderr_buf = ""
        deadline = time.monotonic() + timeout
        try:
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout_buf += resp.read_stdout()
                if resp.peek_stderr():
                    stderr_buf += resp.read_stderr()
                if time.monotonic() > deadline:
                    raise SkillError(f"exec timed out after {timeout}s")
        finally:
            resp.close()
        if stderr_buf.strip():
            logger.warning("exec stderr pod=%s: %s", pod_name, stderr_buf.strip())
        return stdout_buf, resp.returncode
