"""执行沙箱：AI 生成代码的隔离执行环境（Phase 2 加固）。

模式：
- `host`（默认）：直接宿主执行（本地无 docker 时兼容，Phase 0/1 行为）；
- `docker`：在 `platform-sandbox` 镜像中执行——`--network none`（执行期无网络）、
  内存/CPU/PID 限额、以宿主 uid 运行（产物不归 root）、`--rm` 用后即毁。

用法：
    export SANDBOX_MODE=docker
    export SANDBOX_IMAGE=platform-sandbox:latest
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


class HostRunner:
    mode = "host"

    def run(self, cmd: list[str], workdir: str, timeout: int = 300) -> dict:
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"returncode": 124, "stdout": "", "stderr": f"timeout({timeout}s)"}
        return {"returncode": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}

    def python(self) -> str:
        return sys.executable


class DockerSandbox:
    mode = "docker"

    def __init__(
        self,
        image: str | None = None,
        memory: str = "1g",
        cpus: str = "1",
        pids_limit: int = 256,
        network: str = "none",
        timeout: int = 300,
    ):
        self.image = image or os.getenv("SANDBOX_IMAGE", "platform-sandbox:latest")
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.network = network
        self.timeout = timeout

    def run(self, cmd: list[str], workdir: str, timeout: int | None = None) -> dict:
        timeout = timeout or self.timeout
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", self.network,
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{os.path.abspath(workdir)}:/work",
            "-w", "/work",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            self.image,
            *cmd,
        ]
        try:
            proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            return {"returncode": 124, "stdout": "", "stderr": f"sandbox timeout({timeout}s)"}
        except OSError as exc:
            return {"returncode": 125, "stdout": "", "stderr": f"sandbox 启动失败: {exc}"}
        return {"returncode": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}

    def python(self) -> str:
        return "python"  # 容器内解释器


def get_runner():
    """按 SANDBOX_MODE 返回执行器；docker 不可用时回退 host 并告警。"""
    mode = os.getenv("SANDBOX_MODE", "host").lower()
    if mode == "docker":
        if shutil.which("docker"):
            logger.info("沙箱模式：docker（镜像 %s）", os.getenv("SANDBOX_IMAGE", "platform-sandbox:latest"))
            return DockerSandbox()
        logger.warning("SANDBOX_MODE=docker 但未找到 docker，回退 host 模式")
    return HostRunner()
