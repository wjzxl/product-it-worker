"""研发域编码执行器（Phase 1 MVP）。

流程：clone 仓库 → 基于 Issue/PRD/技术方案生成代码 → 本地质量门禁（pytest）→
失败自动迭代修复（上限 max_iterations 轮）→ 提交并推送分支 → 返回分支信息。

实现说明（诚实标注边界）：
- Phase 1 MVP 直接在宿主机执行 git/python（token 经环境变量传入，不落盘文件）；
- 容器沙箱隔离、网络白名单列入 Phase 2 加固项；
- LLM 输出必须为 {"files": {relpath: content}} 纯 JSON，由 LLM 网关（LiteLLM）驱动。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile

from .prompts import get_prompt
from .sandbox import get_runner

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "你是研发域编码 Agent。根据 PRD 与技术方案实现功能。\n"
    "输出必须是纯 JSON，格式：{\"files\": {\"<相对路径>\": \"<文件完整内容>\", ...}}\n"
    "规则：\n"
    "- 只输出 JSON，禁止 markdown 围栏、注释或解释文字；\n"
    "- 只新增/修改必要文件；\n"
    "- 必须包含 pytest 单元测试，确保新功能与既有功能都通过；\n"
    "- 使用 Python 3，代码可直接运行。"
)


class CodingError(RuntimeError):
    pass


class Coder:
    def __init__(
        self,
        llm,
        gitlab_url: str,
        gitlab_token: str,
        project_path: str,
        base_branch: str = "main",
        max_iterations: int = 3,
        workdir: str | None = None,
        push: bool = True,
        runner=None,
        gate_api=None,
    ):
        self.llm = llm
        self.gitlab_url = gitlab_url.rstrip("/")
        self.gitlab_token = gitlab_token
        self.project_path = project_path
        self.base_branch = base_branch
        self.max_iterations = max_iterations
        self.workdir = workdir  # 预置 checkout（测试用）；为空则 clone
        self.push = push
        self.runner = runner or get_runner()  # 执行沙箱（host / docker）
        self.gate_api = gate_api  # 工程闸门客户端（WP2）：E1 在生成代码前拦截

    # ---------- 对外主流程 ----------
    def implement(
        self,
        task: dict,
        prd: str,
        tech_design: str,
        workdir: str | None = None,
        push: bool | None = None,
        branch_prefix: str = "feat/issue-",
        branch_suffix: str = "",
        only_src: bool = False,
        allowed_files: list[str] | None = None,
    ) -> dict:
        issue_ref = task.get("task_ref") or task.get("issue_ref", "")
        iid = str(issue_ref).split("#")[-1] or "0"
        branch = f"{branch_prefix}{iid}{branch_suffix}"

        # WP2 工程闸门 E1「动手之前」：条件不齐时**在调用 LLM / 改动仓库之前**停止。
        # 课程图 40：关键项（影响范围）未知时不得开始修改，不能用"多数项已通过"抵消。
        gate = self._pre_implementation_gate(task, prd, tech_design, only_src=only_src,
                                             allowed_files=allowed_files)
        if gate.get("blocked"):
            raise CodingError(
                "E1 工程闸门未通过，已停止生成代码：\n" + gate.get("_text", "")
            )
        repo = workdir or self.workdir or tempfile.mkdtemp(prefix="coder-")
        push = self.push if push is None else push
        self._repo = repo  # prompt 附带文件全文用
        self._only_src = only_src  # 缺陷修复模式：只允许改 src/（测试文件由测试域定义）
        self._allowed_files = allowed_files  # 缺陷修复模式：仅允许修改这些文件（从失败测试反推）

        if os.path.isdir(os.path.join(repo, ".git")):
            # 复用既有 checkout（测试用），切新分支
            self._git(repo, "checkout", "-b", branch, check=False)
        else:
            self._clone(repo, branch)

        repo_files = self._list_repo(repo)
        feedback = ""
        test_result = {"ok": False, "summary": "", "detail": ""}
        changed: dict = {}
        for attempt in range(1, self.max_iterations + 1):
            prompt = self._build_prompt(task, prd, tech_design, repo_files, feedback)
            system_prompt = get_prompt("coder_agent", DEFAULT_SYSTEM_PROMPT)  # 注册中心热更新
            raw = self.llm.chat(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
            )
            files = self._parse_files(raw)
            if files is None:
                raise CodingError("LLM 输出无法解析为 JSON 文件清单")
            self._apply_files(
                repo, files,
                only_src=getattr(self, "_only_src", False),
                allowed_files=getattr(self, "_allowed_files", None),
            )
            changed = files
            test_result = self._run_tests(repo)
            if test_result["ok"]:
                logger.info("质量门禁通过（attempt=%d, tests=%s）", attempt, test_result["summary"])
                break
            feedback = f"测试失败，请修复（仅输出修复后的完整文件清单 JSON）：\n{test_result['detail'][:2000]}"
            logger.warning("质量门禁未通过（attempt=%d）: %s\n%s", attempt, test_result["summary"], test_result["detail"][-1200:])

        if not test_result["ok"]:
            raise CodingError(
                f"质量门禁在 {self.max_iterations} 轮内未通过：{test_result['summary']}\n{test_result['detail'][-800:]}"
            )

        if push:
            self._commit_push(repo, branch, task, iid)
        return {
            "branch": branch,
            "changed_files": sorted(changed.keys()),
            "test_result": test_result["summary"],
        }

    # ---------- 内部实现 ----------
    # ---------- 工程闸门（WP2） ----------
    def _pre_implementation_gate(self, task: dict, prd: str, tech_design: str,
                                 only_src: bool = False,
                                 allowed_files: list[str] | None = None) -> dict:
        """调用 E1 工程闸门，返回判定结果（附 `_text` 供报错信息使用）。

        上下文里**能自动推断的都填上**，只把真正需要人给的部分留给闸门判缺口：
        - prd_ref / sources：来自任务与 PRD（需求依据）
        - change_targets：缺陷修复模式下来自失败测试反推的文件；否则由 task 显式提供
        - impact_scope / rollback_plan：必须由调用方给（这正是 E1 要拦的地方）
        """
        if self.gate_api is None:
            return {"blocked": False, "passed": True, "mode": "off", "gaps": [], "_text": ""}
        context = {
            "prd_ref": (prd or "")[:200] if prd else task.get("prd_ref", ""),
            "sources": task.get("sources") or [],
            "change_targets": allowed_files or (["src/**（缺陷修复范围）"] if only_src else task.get("change_targets") or []),
            "impact_scope": task.get("impact_scope", ""),
            "rollback_plan": task.get("rollback_plan", ""),
        }
        result = self.gate_api.check(
            "E1",
            task_ref=str(task.get("task_ref") or task.get("issue_ref") or ""),
            context=context,
            mode=task.get("gate_mode") or None,
            checked_by="agent:coder",
        )
        try:
            from .gate_text import render_gaps  # 本地渲染，避免跨服务依赖

            result["_text"] = render_gaps(result)
        except Exception:  # 渲染失败不影响判定
            result["_text"] = str(result.get("gaps"))
        logger.info("E1 闸门：passed=%s blocked=%s gaps=%s", result.get("passed"), result.get("blocked"),
                    [g.get("id") for g in result.get("gaps") or []])
        return result

    def _clone_url(self) -> str:
        host = self.gitlab_url.split("://")[1]
        return f"https://oauth2:{self.gitlab_token}@{host}/{self.project_path}.git"

    def _clone(self, repo: str, branch: str) -> None:
        self._run(
            ["git", "clone", "--depth=1", self._clone_url(), repo],
            cwd=os.path.dirname(repo) or ".",
        )
        self._git(repo, "checkout", "-b", branch)

    def _build_prompt(self, task: dict, prd: str, tech_design: str, repo_files: list[str], feedback: str) -> str:
        rule = (
            "\n# 约束（缺陷修复模式）\n测试文件已存在且由测试域定义，**禁止修改或新增 tests/ 下任何文件**；"
            "只输出 src/ 下实现代码的修改。"
            if getattr(self, "_only_src", False)
            else ""
        )
        return (
            f"# 任务\n标题：{task.get('title', '')}\n引用：{task.get('task_ref', '')}\n"
            f"项目路径：{self.project_path}\n\n"
            f"# PRD（验收标准）\n{prd[:6000]}\n\n"
            f"# 技术方案\n{tech_design[:4000]}\n\n"
            f"# 仓库现状文件\n{chr(10).join(repo_files)}\n\n"
            f"# 相关文件内容（外科手术式编辑：基于真实内容做最小修改，禁止用占位注释替换函数体）\n"
            f"{self._related_sources(repo_files)}\n\n"
            + (f"# 上一轮测试反馈（必须解决）\n{feedback}\n" if feedback else "")
            + rule
            + "\n请输出实现（含测试）的文件清单 JSON。"
        )

    def _related_sources(self, repo_files: list[str]) -> str:
        """小仓库附带 src 文件全文，帮助模型做精确修改（总长受限）。"""
        if getattr(self, "_repo", None) is None:
            return "（无）"
        parts: list[str] = []
        total = 0
        for rel in sorted(repo_files):
            if not rel.startswith("src/") or not rel.endswith(".py"):
                continue
            path = os.path.join(self._repo, rel)
            try:
                content = open(path, encoding="utf-8").read()
            except OSError:
                continue
            if total + len(content) > 10000:
                continue
            parts.append(f"--- {rel} ---\n{content}")
            total += len(content)
        return "\n".join(parts) or "（无）"

    @staticmethod
    def _parse_files(raw: str) -> dict | None:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict) or "files" not in data:
            return None
        files = data["files"]
        if not isinstance(files, dict):
            return None
        cleaned = {}
        for rel, content in files.items():
            rel = str(rel).lstrip("./")
            if rel.startswith("/") or ".." in rel.split("/"):
                continue
            cleaned[rel] = str(content)
        return cleaned

    @staticmethod
    def _apply_files(repo: str, files: dict, only_src: bool = False, allowed_files: list[str] | None = None) -> None:
        # 保护关键配置文件：AI 不允许覆盖（否则会破坏构建/测试配置）
        protected = {".gitignore", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", ".gitlab-ci.yml", "README.md"}
        for rel, content in files.items():
            base = rel.split("/")[0]
            if rel in protected:
                logger.warning("[coder] 跳过保护文件: %s", rel)
                continue
            if only_src and not rel.startswith("src/"):
                logger.warning("[coder] 缺陷修复模式：跳过非 src 文件: %s", rel)
                continue
            if allowed_files is not None and rel not in allowed_files:
                logger.warning("[coder] 缺陷修复模式：跳过非关联文件: %s", rel)
                continue
            if base not in ("src", "tests") and rel in _existing(repo):
                logger.warning("[coder] 不允许覆盖既有非代码文件: %s", rel)
                continue
            path = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    @staticmethod
    def _existing(repo: str) -> set:
        return set(Coder._list_repo(repo))

    @staticmethod
    def _list_repo(repo: str) -> list[str]:
        files: list[str] = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
            for name in sorted(names):
                full = os.path.join(root, name)
                files.append(os.path.relpath(full, repo))
        return files

    def _run_tests(self, repo: str) -> dict:
        """质量门禁：在沙箱中执行 pytest（SANDBOX_MODE=docker 时 AI 代码不出容器）。"""
        res = self.runner.run([self.runner.python(), "-m", "pytest", "-q"], workdir=repo, timeout=300)
        stdout = res["stdout"]
        summary = stdout.strip().splitlines()[-1] if stdout.strip() else "无输出"
        detail = ((res["stderr"] or "") + stdout)[-3000:]
        if res["returncode"] == 124:
            summary = "测试超时(300s)"
        return {"ok": res["returncode"] == 0, "summary": summary, "detail": detail}

    def _commit_push(self, repo: str, branch: str, task: dict, iid: str) -> None:
        self._git(repo, "add", "-A")
        # 注意：-c 配置必须放在子命令之前（git -c k=v commit）
        self._run(
            [
                "git",
                "-c", "user.name=AI Dev Agent",
                "-c", "user.email=dev-agent@platform.local",
                "commit", "-m", f"feat: {task.get('title', '')} (#{iid})",
            ],
            cwd=repo,
        )
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        self._run(
            ["git", "push", "-u", "origin", branch],
            cwd=repo, env=env, check=False,
        )

    def _git(self, repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(["git", *args], cwd=repo, check=check)

    @staticmethod
    def _run(cmd: list[str], cwd: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        env = env or dict(os.environ, GIT_TERMINAL_PROMPT="0")
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=300)
        if check and proc.returncode != 0:
            raise CodingError(f"{' '.join(cmd)} 失败: {(proc.stderr or proc.stdout)[-500:]}")
        return proc
