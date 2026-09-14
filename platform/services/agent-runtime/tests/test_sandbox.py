"""沙箱执行器测试：host 模式 + docker 模式隔离性（docker 不可用时跳过）。"""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from app.coder import Coder
from app.llm import MockLLM
from app.sandbox import DockerSandbox, HostRunner, get_runner


class RecordingRunner:
    """记录调用命令的桩执行器（验证 coder/tester 是否走沙箱）。"""

    mode = "docker"

    def __init__(self):
        self.commands: list[list[str]] = []

    def run(self, cmd, workdir, timeout=300):
        self.commands.append(list(cmd))
        return {"returncode": 0, "stdout": "1 passed in 0.01s", "stderr": ""}

    def python(self):
        return "python"


def _repo() -> str:
    import subprocess

    d = tempfile.mkdtemp(prefix="sandboxrepo-")
    (Path(d) / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    (Path(d) / "src").mkdir()
    (Path(d) / "src" / "__init__.py").write_text("")
    (Path(d) / "tests").mkdir()
    (Path(d) / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "init"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "base"], cwd=d, check=True, capture_output=True)
    return d


def test_coder_uses_injected_sandbox():
    """coder 的质量门禁必须经由注入的执行器（即沙箱）。"""
    runner = RecordingRunner()
    llm = MockLLM(['{"files": {"src/app.py": "def f():\\n    return 1\\n"}}'])
    coder = Coder(
        llm=llm, gitlab_url="https://gitlab.com", gitlab_token="tok",
        project_path="sandbox/prodesgin", workdir=_repo(), push=False, runner=runner,
    )
    result = coder.implement(task={"task_ref": "1#1", "title": "t"}, prd="p", tech_design="d")
    assert result["test_result"] == "1 passed in 0.01s"
    assert runner.commands and runner.commands[0][0] == "python"  # 容器内解释器
    assert "-m" in runner.commands[0] and "pytest" in runner.commands[0]


def test_host_runner_runs_real_pytest():
    repo = _repo()
    res = HostRunner().run([HostRunner().python(), "-m", "pytest", "-q"], workdir=repo)
    assert res["returncode"] == 0 and "1 passed" in res["stdout"]


def test_get_runner_defaults_to_host(monkeypatch):
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    assert get_runner().mode == "host"


@pytest.mark.skipif(not shutil.which("docker"), reason="需要 docker")
def test_docker_sandbox_isolation():
    """docker 沙箱：容器内执行 + 执行期无网络。"""
    repo = _repo()
    sandbox = DockerSandbox()
    probe = sandbox.run(
        ["python", "-c", "import os,socket;print('IN_CONTAINER', os.path.exists('/.dockerenv'));"
                          "\ntry:\n socket.create_connection(('1.1.1.1',80),timeout=3); print('NET_OK')\n"
                          "except OSError: print('NET_BLOCKED')"],
        workdir=repo,
    )
    assert probe["returncode"] == 0, probe["stderr"][-300:]
    assert "IN_CONTAINER True" in probe["stdout"]
    assert "NET_BLOCKED" in probe["stdout"]
    # 真实 pytest 在容器内运行
    res = sandbox.run(["python", "-m", "pytest", "-q"], workdir=repo)
    assert res["returncode"] == 0 and "1 passed" in res["stdout"]
