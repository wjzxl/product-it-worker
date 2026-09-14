"""运维域 SOP 测试：告警诊断 → G5 变更审批 → 执行（修复合入/操作记录）→ 复盘 → G6 审查。

fix_code 路径使用真实 git 临时仓库 + 真实 pytest（push=False），
LLM 用 MockLLM（诊断 JSON/修复代码/复盘），GitLab 用 MockGitLabAPI。
"""
import json
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.apis import MockApprovalAPI, MockGitLabAPI, MockKnowledgeAPI
from app.coder import Coder
from app.llm import MockLLM
from app.main import create_app

QUERY_SRC = '''"""报表查询模块。"""
from datetime import date


def get_report_data(start_date: date, end_date: date, rows: list[dict]) -> list[dict]:
    return [r for r in rows if start_date <= date.fromisoformat(r["日期"]) <= end_date]
'''


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _make_repo() -> str:
    d = tempfile.mkdtemp(prefix="opsrepo-")
    (Path(d) / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    (Path(d) / "src" / "reports").mkdir(parents=True)
    (Path(d) / "src" / "reports" / "__init__.py").write_text("")
    (Path(d) / "src" / "reports" / "query.py").write_text(QUERY_SRC)
    (Path(d) / "tests").mkdir()
    (Path(d) / "tests" / "test_query.py").write_text(
        "from datetime import date\nfrom reports.query import get_report_data\n\n"
        "def test_filter():\n"
        "    rows = [{'日期': '2024-01-01'}, {'日期': '2024-02-01'}]\n"
        "    assert len(get_report_data(date(2024,1,1), date(2024,1,31), rows)) == 1\n"
    )
    _git(d, "init")
    _git(d, "add", "-A")
    _git(d, "commit", "-m", "base")
    return d


def _diag(change_type: str) -> str:
    return json.dumps(
        {
            "root_cause": "导出接口在空日期范围时返回 500",
            "impact": "报表导出不可用",
            "change_type": change_type,
            "change_summary": "空日期范围返回空列表",
            "risk_level": "L3",
        },
        ensure_ascii=False,
    )


def _fix_json() -> str:
    fixed = (
        "from datetime import date\n\n\n"
        "def get_report_data(start_date, end_date, rows):\n"
        "    if start_date is None or end_date is None:\n"
        "        return []\n"
        "    return [r for r in rows if start_date <= date.fromisoformat(r['日期']) <= end_date]\n"
    )
    return json.dumps({"files": {"src/reports/query.py": fixed}}, ensure_ascii=False)


def _env(answers: list[str]):
    llm = MockLLM(answers)
    approval = MockApprovalAPI()
    gitlab = MockGitLabAPI()
    repo = _make_repo()
    coder = Coder(llm=llm, gitlab_url="https://gitlab.com", gitlab_token="tok", project_path="sandbox/prodesgin", workdir=repo, push=False)
    knowledge = MockKnowledgeAPI()
    app = create_app(approval_api=approval, gitlab_api=gitlab, llm=llm, coder=coder, knowledge_api=knowledge)
    return {"client": TestClient(app), "approval": approval, "gitlab": gitlab, "repo": repo, "knowledge": knowledge}


def _start(env, task_ref="alert-1"):
    return env["client"].post(
        "/runs",
        json={
            "sop": "ops-domain/alert-to-postmortem",
            "requirement_text": "告警处置",
            "title": "导出接口 5xx",
            "task_ref": task_ref,
            "project_id": "sandbox/prodesgin",
            "alert": {"id": task_ref, "title": "导出接口 5xx 错误率上升", "severity": "P1", "source": "prometheus"},
            "context": "2024-01-01 10:00 导出接口 5xx 率 25%，日志：ValueError: start_date is None",
            "runbook": "导出接口 5xx：检查日期参数；空日期范围返回 400/空列表；必要时回滚发布。",
        },
    )


class TestOpsFlow:
    def test_fix_code_full_loop(self):
        env = _env([_diag("fix_code"), _fix_json(), "复盘：根因空日期未处理，已修复合入，改进项补充边界测试。"])
        r = _start(env)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g5"
        assert body["approval_id"].startswith("apv_mock")
        assert body["change_type"] == "fix_code"
        # G5 三要素：生产变更 L3 强制审批
        payload = env["approval"].requests[-1]
        assert payload["type"] == "GATE_CHANGE_APPROVAL"
        assert payload["context_snapshot"]["risk"]["level"] == "L3"
        # Runbook RAG：知识库检索被调用且来源进入审批依据（引用留痕）
        assert env["knowledge"].queries, "应触发 Runbook 检索"
        assert any("export-performance.md" in e for e in payload["context_snapshot"]["evidence"])
        assert body["runbook_sources"] == ["export-performance.md"]

        r = env["client"].post(f"/runs/alert-1/resume", json={"decision": "approve", "comment": "变更合规，放行"})
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g6", body
        assert "fix_code 已合入" in body.get("execution", "")
        assert body["postmortem"]

        r = env["client"].post(f"/runs/alert-1/resume", json={"decision": "approve", "comment": "复盘通过"})
        body = r.json()
        assert body["status"] == "done"
        # 修复真实写入仓库 + 缺陷路径：变更单为 fix_code 走 MR（push=False 时仍记录执行）
        assert (Path(env["repo"]) / "src/reports/query.py").read_text().startswith("from datetime")

    def test_operational_change_records_issue(self):
        env = _env([_diag("operational"), "复盘：操作类变更（重启）已执行，无代码变更。"])
        r = _start(env, task_ref="alert-2")
        body = r.json()
        assert body["change_type"] == "operational"
        r = env["client"].post(f"/runs/alert-2/resume", json={"decision": "approve", "comment": "放行"})
        body = r.json()
        assert body["gate"] == "gate_g6"
        assert "operational" in body.get("execution", "")
        # 变更记录单（change 标签）
        assert any("change" in c["labels"] for c in env["gitlab"].created)
        r = env["client"].post(f"/runs/alert-2/resume", json={"decision": "approve", "comment": "OK"})
        assert r.json()["status"] == "done"

    def test_g5_reject(self):
        env = _env([_diag("operational"), "复盘。"])
        r = _start(env, task_ref="alert-3")
        tid = r.json()["thread_id"]
        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "reject", "comment": "变更窗口未批准"})
        body = r.json()
        assert body["status"] == "rejected"
        assert body["comment"] == "变更窗口未批准"

    def test_g6_reject_after_execute(self):
        env = _env([_diag("operational"), "复盘：常规告警。"])
        r = _start(env, task_ref="alert-4")
        tid = r.json()["thread_id"]
        env["client"].post(f"/runs/{tid}/resume", json={"decision": "approve", "comment": "放行"})
        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "reject", "comment": "复盘结论需补充影响面"})
        assert r.json()["status"] == "rejected"
