"""测试域执行引擎（Phase 2 MVP）。

流程：
1. clone 最新 main → 依据 PRD/验收标准生成**独立**测试用例（分层：单元=函数级、集成=模块级调用链）；
2. 执行（pytest）；失败则进入**缺陷闭环**：
   - 真实创建缺陷 Issue（标签 defect/auto-test）
   - AI 修复（复用 Coder：修复分支 fix/issue-{iid} → push → MR → 合入）
   - 重新回归（真实模式：重新 clone main 复跑；测试模式：就地复跑）
   - 通过则关闭缺陷单，否则保留待人工
3. 返回测试结果与缺陷清单，供报告节点汇总并进入 G4 放行决策。

诚实标注：库型试点项目以 pytest 分层（单元/集成）；Web E2E（Playwright）留待前端类项目。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile

from .coder import Coder, CodingError
from .sandbox import get_runner

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是测试域 Agent。依据 PRD 的验收标准，为已合入的代码生成**独立**的测试用例。\n"
    "输出必须是纯 JSON：{\"files\": {\"<相对路径>\": \"<文件内容>\", ...}}\n"
    "规则：\n"
    "- 只输出 JSON，禁止围栏/注释/解释；\n"
    "- 分层生成：单元测试（函数级边界）tests/test_{feature}_unit.py、"
    "集成测试（模块级调用链）tests/test_{feature}_integration.py；\n"
    "- 测试必须可运行（pytest），覆盖验收标准的每条 WHEN...THEN；\n"
    "- **只针对目标功能新增新文件，禁止输出任何仓库中已存在的文件路径**（不得覆盖/修改既有文件）；\n"
    "- 不得修改 src/ 下的生产代码。"
)


class TestEngine:
    def __init__(
        self,
        llm,
        coder: Coder,
        gitlab_api,
        gitlab_url: str,
        gitlab_token: str,
        project_path: str,
        max_defect_rounds: int = 1,
        push: bool = True,
        workdir: str | None = None,
    ):
        self.llm = llm
        self.coder = coder
        self.gitlab_api = gitlab_api
        self.gitlab_url = gitlab_url.rstrip("/")
        self.gitlab_token = gitlab_token
        self.project_path = project_path
        self.max_defect_rounds = max_defect_rounds
        self.push = push  # False = 测试模式（就地修复复跑，不 push/MR/合入）
        self.workdir = workdir  # 预置 checkout（测试用）；为空则 clone
        self.runner = get_runner()  # 执行沙箱（host / docker）

    def run(
        self,
        task: dict,
        prd: str,
        workdir: str | None = None,
    ) -> dict:
        issue_ref = task.get("task_ref") or task.get("issue_ref", "")
        iid = str(issue_ref).split("#")[-1] or "0"
        feature = _slug(task.get("title", "feature"))
        repo = workdir or self.workdir or tempfile.mkdtemp(prefix="tester-")
        self._checkout_main(repo)

        # 1. 生成独立测试用例（先在隔离的"仅生成文件"目标下自检收集，再全仓回归）
        files = self._list_repo(repo)
        api_map = self._list_api(repo)
        prompt = (
            f"# 被测功能\n标题：{task.get('title', '')}\n引用：{issue_ref}\n项目：{self.project_path}\n\n"
            f"# PRD（验收标准）\n{prd[:6000]}\n\n"
            f"# 仓库现状文件\n{chr(10).join(files)}\n\n"
            f"# 可用 API（必须从这些真实路径导入，禁止猜测模块名）\n{chr(10).join(api_map)}\n\n"
            f"请生成独立测试用例（feature 名：{feature}），输出文件清单 JSON。"
        )
        test_files: dict = {}
        written: list[str] = []
        result = {"ok": False, "summary": "", "detail": ""}
        for regen in range(3):  # 收集错误自动重生成（最多 3 次）
            raw = self.llm.chat(
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
            )
            test_files = self._parse_files(raw)
            if not test_files:
                raise CodingError("测试用例生成输出无法解析为 JSON")
            written = self._apply_files(repo, test_files)
            if not written:
                # 生成内容全部命中既有文件（LLM 违规输出重复路径）→ 视为生成失败
                result = {"ok": False, "summary": "用例生成失败（输出均为既有文件路径）", "detail": prompt[-1500:]}
                break
            # 隔离自检：只收集本次生成的文件（任何 import/语法错误都在这里暴露）
            result = self._pytest(repo, targets=written)
            if result["ok"] or not _is_collection_error(result["detail"]):
                break
            prompt += f"\n# 上一轮失败反馈（必须修正）\n{result['detail'][-1500:]}"
            for path in written:
                if os.path.exists(path):
                    os.remove(path)
            logger.warning("[tester] 用例收集错误，重生成（regen=%d）", regen + 1)

        # 2. 隔离自检未过 → 生成质量问题，如实上报，不进缺陷闭环
        if not result["ok"] and _is_collection_error(result["detail"]):
            result = {
                "ok": False,
                "summary": "用例生成失败（收集错误，重生成后仍失败）",
                "detail": result["detail"][-2000:],
            }
            logger.error("[tester] 用例生成收集错误，跳过缺陷闭环（避免误建缺陷单）")

        # 3. 全仓回归：生成用例 + 既有用例一起跑（既有文件未被触碰）
        if result["ok"]:
            result = self._pytest(repo)
            if not result["ok"] and _is_collection_error(result["detail"]):
                result = {
                    "ok": False,
                    "summary": "全仓回归收集错误（既有测试受损？）",
                    "detail": result["detail"][-2000:],
                }
                logger.error("[tester] 全仓回归收集错误：%s", result["detail"][-800:])

        # 4. 真实断言失败（非收集错误）→ 进入缺陷闭环
        defects: list[dict] = []
        rounds_used = 0
        if not result["ok"] and self.max_defect_rounds > 0 and not _is_collection_error(result["detail"]):
            rounds_used = self._defect_loop(repo, task, iid, feature, prd, test_files, result, defects)
            # 缺陷修复后回归
            result = self._pytest(repo)
            if result["ok"] and defects and defects[-1].get("fixed") and defects[-1].get("issue_iid"):
                try:
                    self.gitlab_api.close_issue(self.project_path, defects[-1]["issue_iid"])
                    defects[-1]["closed"] = True
                except Exception as exc:
                    logger.warning("缺陷单关闭失败: %s", exc)

        return {
            "test_files": sorted(test_files.keys()),
            "result": result,
            "defects": defects,
            "defect_rounds": rounds_used,
        }

    def _defect_loop(self, repo, task, iid, feature, prd, test_files, result, defects) -> int:
        """缺陷闭环：建缺陷单 → AI 修复（限定关联文件）→（真实模式）修复分支 MR 合入 → 回归。返回消耗轮数。"""
        for round_no in range(1, self.max_defect_rounds + 1):
            detail = f"独立测试失败（第 {round_no} 轮）：\n{result['detail'][:2000]}"
            try:
                defect = self.gitlab_api.create_issue(
                    self.project_path,
                    title=f"缺陷: {task.get('title', '')} 独立测试失败",
                    description=detail,
                    labels=["defect", "auto-test"],
                )
            except Exception as exc:  # 建单失败不阻断（记录）
                logger.warning("缺陷建单失败: %s", exc)
                defect = {"iid": None}
            defects.append({"issue_iid": defect.get("iid"), "fixed": False, "closed": False})

            # AI 修复（反馈失败输出；仅允许修改失败测试直接引用的 src 文件）
            fix_branch = f"fix/issue-{iid}-t{round_no}"
            allowed = self._allowed_fix_files(repo, result["detail"])
            try:
                self.coder.implement(
                    task=task,
                    prd=prd,
                    tech_design=detail,
                    branch_prefix="fix/issue-",
                    branch_suffix=f"-t{round_no}",
                    push=self.push,
                    workdir=repo,
                    only_src=True,  # 缺陷修复模式：只改 src/，测试文件由测试域定义
                    allowed_files=allowed,
                )
            except CodingError as exc:
                logger.error("缺陷修复失败（第 %d 轮）: %s", round_no, exc)
                return round_no  # 缺陷保留待人工

            if self.push:
                # 真实模式：修复分支 → MR → 合入 → 重新 clone 回归
                mr = self.gitlab_api.create_mr(
                    self.project_path, fix_branch, "main",
                    title=f"fix: {task.get('title', '')} 独立测试失败",
                    description=f"独立测试缺陷修复（关联 #{defect.get('iid') or ''}）",
                )
                mr_iid = mr.get("iid")
                mr_info = self.gitlab_api.get_mr(self.project_path, mr_iid)
                self.gitlab_api.merge_mr(self.project_path, mr_iid, sha=mr_info.get("sha"))
                self._checkout_main(repo, reset=True)  # 重新拉最新 main（含修复）
                self._apply_files(repo, test_files)  # 重新应用独立测试
            defects[-1]["fixed"] = True
            return round_no
        return self.max_defect_rounds

    # ---------- 基础设施 ----------
    def _clone_url(self) -> str:
        host = self.gitlab_url.split("://")[1]
        return f"https://oauth2:{self.gitlab_token}@{host}/{self.project_path}.git"

    def _checkout_main(self, repo: str, reset: bool = False) -> None:
        if os.path.isdir(os.path.join(repo, ".git")) and not reset:
            return  # 预置 checkout（测试用）
        if reset and os.path.isdir(os.path.join(repo, ".git")):
            self._run(["git", "fetch", "origin", "main"], cwd=repo)
            self._run(["git", "checkout", "main"], cwd=repo, check=False)
            self._run(["git", "reset", "--hard", "origin/main"], cwd=repo)
            return
        self._run(["git", "clone", "--depth=1", self._clone_url(), repo], cwd=os.path.dirname(repo) or ".")

    def _pytest(self, repo: str, targets: list[str] | None = None) -> dict:
        """分层执行：pytest 在沙箱中运行（SANDBOX_MODE=docker 时 AI 用例不出容器）。"""
        cmd = [self.runner.python(), "-m", "pytest", "-q"]
        if targets:
            cmd += targets  # 隔离自检：只收集本次生成的文件
        res = self.runner.run(cmd, workdir=repo, timeout=300)
        stdout = res["stdout"]
        summary = stdout.strip().splitlines()[-1] if stdout.strip() else "无输出"
        if res["returncode"] == 124:
            summary = "测试超时(300s)"
        return {
            "ok": res["returncode"] == 0,
            "summary": summary,
            "detail": ((res["stderr"] or "") + stdout)[-3000:],
        }

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
        files = data.get("files") if isinstance(data, dict) else None
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
    def _apply_files(repo: str, files: dict) -> list[str]:
        """只新增独立用例，绝不覆盖/删除仓库既有文件；返回实际写入的路径列表。"""
        protected = {"pyproject.toml", ".gitignore", "README.md"}
        written: list[str] = []
        for rel, content in files.items():
            path = os.path.join(repo, rel)
            if rel in protected:
                logger.warning("[tester] 跳过保护文件: %s", rel)
                continue
            if os.path.exists(path):
                # 测试域只新增独立用例，绝不覆盖既有文件（防止破坏仓库既有测试）
                logger.warning("[tester] 跳过既有文件（不覆盖）: %s", rel)
                continue
            os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(path)
        return written

    @staticmethod
    def _allowed_fix_files(repo: str, detail: str) -> list[str]:
        """从 pytest 失败输出反推允许修改的 src 文件：
        解析失败测试文件（tests/*.py）的 import，映射到 src 下的模块路径。"""
        import ast
        import re

        allowed: set[str] = set()
        test_paths = set(re.findall(r"tests/[A-Za-z0-9_./-]+\.py", detail))
        for test_rel in test_paths:
            test_abs = os.path.join(repo, test_rel)
            if not os.path.exists(test_abs):
                continue
            try:
                tree = ast.parse(open(test_abs, encoding="utf-8").read())
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("src."):
                        mod = node.module.replace(".", "/")
                    elif node.module.startswith("reports."):  # pythonpath=src 下的模块
                        mod = "src/" + node.module.replace(".", "/")
                    else:
                        continue
                    allowed.add(f"{mod}.py")
                    allowed.add(f"{mod}/__init__.py")
        return sorted(allowed) or None  # 解析不到 → 不设文件级限制（仍受 only_src 约束）

    @staticmethod
    def _list_api(repo: str) -> list[str]:
        """AST 提取仓库真实可导入 API（src/ 下模块的顶层函数/类），防止 LLM 猜测模块名。"""
        import ast

        lines: list[str] = []
        for root, dirs, names in os.walk(os.path.join(repo, "src")):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv")]
            for name in sorted(names):
                if not name.endswith(".py") or name == "__init__.py":
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, repo).replace(os.sep, "/")
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except SyntaxError:
                    continue
                symbols = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
                if symbols:
                    lines.append(f"{rel}: {', '.join(symbols)}")
        return lines

    @staticmethod
    def _list_repo(repo: str) -> list[str]:
        files = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
            for name in sorted(names):
                files.append(os.path.relpath(os.path.join(root, name), repo))
        return files

    @staticmethod
    def _run(cmd: list[str], cwd: str, check: bool = True) -> None:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=300)
        if check and proc.returncode != 0:
            raise CodingError(f"{' '.join(cmd)} 失败: {(proc.stderr or proc.stdout)[-500:]}")


def _slug(title: str) -> str:
    import re

    return re.sub(r"[^0-9a-zA-Z_]+", "_", title).strip("_").lower() or "feature"


def _is_collection_error(detail: str) -> bool:
    """判断 pytest 输出是否主要为收集/导入类错误（而非断言失败）。"""
    markers = ("ImportError", "SyntaxError", "ModuleNotFoundError", "error during collection", "cannot import name")
    return any(m in detail for m in markers) and "failed" not in detail.splitlines()[-3:][0] if detail else False
