"""研发域 SOP 测试：G2 方案评审 → AI 编码（真实 git+pytest）→ MR → G3 → 合入。

使用真实 git 临时仓库与真实 pytest 运行质量门禁（sys.executable 的 venv 内含 pytest），
LLM 用 MockLLM 提供技术方案与代码 JSON，GitLab 用 MockGitLabAPI。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.apis import MockApprovalAPI, MockGitLabAPI
from app.coder import Coder
from app.llm import MockLLM
from app.main import create_app

QUERY_SRC = '''"""报表查询模块（试点基线）。"""
from datetime import date


def get_report_data(start_date: date, end_date: date, rows: list[dict]) -> list[dict]:
    """按日期范围过滤报表数据行。"""
    return [r for r in rows if start_date <= date.fromisoformat(r["日期"]) <= end_date]
'''

QUERY_TEST = '''from datetime import date

from reports.query import get_report_data


def test_filter():
    rows = [
        {"日期": "2024-01-01", "类别": "营收", "数值": 1},
        {"日期": "2024-02-01", "类别": "营收", "数值": 2},
    ]
    out = get_report_data(date(2024, 1, 1), date(2024, 1, 31), rows)
    assert len(out) == 1 and out[0]["日期"] == "2024-01-01"
'''

EXPORTER_SRC = '''"""CSV 导出实现（AI 生成）。"""
import csv
import io


def export_csv(data: list[dict], start_date: str = "", end_date: str = "") -> str:
    rows = [
        r for r in data
        if (not start_date or r["日期"] >= start_date)
        and (not end_date or r["日期"] <= end_date)
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["日期", "类别", "数值", "备注"], lineterminator="\\n")
    writer.writeheader()
    writer.writerows(rows)
    return "\\ufeff" + out.getvalue()
'''

EXPORTER_TEST = '''from reports.exporter import export_csv


def test_export_with_filter():
    data = [
        {"日期": "2024-01-01", "类别": "营收", "数值": 1, "备注": "a"},
        {"日期": "2024-02-01", "类别": "营收", "数值": 2, "备注": "b"},
    ]
    out = export_csv(data, "2024-01-01", "2024-01-31")
    assert "\\ufeff" in out          # BOM
    assert "2024-01-01" in out
    assert "2024-02-01" not in out   # 日期过滤生效
    assert "日期,类别,数值,备注" in out
'''


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _make_repo() -> str:
    d = tempfile.mkdtemp(prefix="devrepo-")
    (Path(d) / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    (Path(d) / "src" / "reports").mkdir(parents=True)
    (Path(d) / "src" / "reports" / "__init__.py").write_text("")
    (Path(d) / "src" / "reports" / "query.py").write_text(QUERY_SRC)
    (Path(d) / "tests").mkdir()
    (Path(d) / "tests" / "test_query.py").write_text(QUERY_TEST)
    _git(d, "init")
    _git(d, "add", "-A")
    _git(d, "commit", "-m", "base")
    return d


def _code_json() -> str:
    return json.dumps({"files": {"src/reports/exporter.py": EXPORTER_SRC, "tests/test_exporter.py": EXPORTER_TEST}}, ensure_ascii=False)


def _env(answers: list[str]):
    llm = MockLLM(answers)
    approval = MockApprovalAPI()
    gitlab = MockGitLabAPI()
    repo = _make_repo()
    coder = Coder(llm=llm, gitlab_url="https://gitlab.com", gitlab_token="tok", project_path="sandbox/prodesgin", workdir=repo, push=False)
    app = create_app(approval_api=approval, gitlab_api=gitlab, llm=llm, coder=coder)
    return {"client": TestClient(app), "approval": approval, "gitlab": gitlab, "repo": repo, "llm": llm}


def _start(env, task_ref="42#1"):
    return env["client"].post(
        "/runs",
        json={
            "sop": "dev-domain/issue-to-merge",
            "requirement_text": "报表导出功能：支持按日期筛选导出 CSV",
            "title": "报表导出功能",
            "task_ref": task_ref,
            "project_id": "sandbox/prodesgin",
            "prd_doc": "## 验收标准\n- WHEN 用户按日期筛选 THEN 导出 CSV 含 BOM\n- WHEN 日期范围无数据 THEN 仍输出表头",
        },
    )


class TestDevFlow:
    def test_approve_g2_g3_merge(self):
        env = _env(["技术方案：新增 exporter 模块，CSV 导出，UTF-8 BOM，日期过滤。", _code_json()])
        r = _start(env)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g2"
        assert body["approval_id"].startswith("apv_mock")
        assert "技术方案" in env["approval"].requests[-1]["context_snapshot"]["diff_summary"]

        r = env["client"].post(f"/runs/42-1/resume", json={"decision": "approve", "comment": "方案可行"})
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g3", body
        assert body["mr_iid"] == 1 and body["mr_url"]
        assert "passed" in body["test_result"]
        # 质量门禁真实执行：exporter 与测试文件已写入临时仓库
        assert (Path(env["repo"]) / "src/reports/exporter.py").exists()

        r = env["client"].post(f"/runs/42-1/resume", json={"decision": "approve", "comment": "LGTM"})
        body = r.json()
        assert body["status"] == "done" and body["merged"] is True
        assert env["gitlab"].merged[0]["iid"] == 1
        assert env["gitlab"].created_mrs[0]["source_branch"] == "feat/issue-1"

    def test_g2_revise_then_approve(self):
        # answers: design1, design2(修订), code
        env = _env(["技术方案 v1：导出模块。", "技术方案 v2：补充接口定义。", _code_json()])
        r = _start(env)
        tid = r.json()["thread_id"]
        aid1 = r.json()["approval_id"]

        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "revise", "comment": "补充接口定义"})
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g2"
        assert body["approval_id"] != aid1  # 修订后重新发起 G2

        env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "OK"})
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "LGTM"})
        done = env["client"].get(f"/runs/{tid}/state").json()
        assert done["values"].get("merged") is True

    def test_g3_reject_no_merge(self):
        env = _env(["技术方案：导出模块。", _code_json()])
        r = _start(env, task_ref="42#2")
        tid = r.json()["thread_id"]
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "OK"})
        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "reject", "comment": "实现不符合验收标准"})
        body = r.json()
        assert body["status"] == "rejected"
        assert env["gitlab"].merged == []  # 否决后不合入

    def test_g3_revise_rework_then_merge(self):
        # answers: design, code1(首版), code2(返工后)
        env = _env(["技术方案：导出模块。", _code_json(), _code_json()])
        r = _start(env, task_ref="42#3")
        tid = r.json()["thread_id"]
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "OK"})
        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "revise", "comment": "补充空数据场景测试"})
        assert r.json()["status"] == "waiting_approval" and r.json()["gate"] == "gate_g3"
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "LGTM"})
        done = env["client"].get(f"/runs/{tid}/state").json()
        assert done["values"].get("merged") is True
        assert len(env["gitlab"].created_mrs) == 1  # MR 只创建一个（返工不重复建 MR）
