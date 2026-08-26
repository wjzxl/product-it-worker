"""测试域 SOP 测试：计划 → 独立用例生成 → 执行 → 缺陷闭环 → 报告 → G4 放行。

真实 pytest 执行于临时 git 仓库（push=False 就地修复复跑），
LLM 用 MockLLM（计划/用例/修复），GitLab 用 MockGitLabAPI。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.apis import MockApprovalAPI, MockGitLabAPI
from app.coder import Coder
from app.llm import MockLLM
from app.main import create_app
from app.tester import TestEngine

SUMMARY_SRC = '''"""按类别汇总（试点功能）。"""


def summarize_by_category(data: list[dict]) -> dict:
    """按类别聚合数值。"""
    out: dict = {}
    for row in data:
        out[row["类别"]] = out.get(row["类别"], 0) + row["数值"]
    return out
'''

BASELINE_TEST = '''from reports.summary import summarize_by_category


def test_aggregate():
    data = [
        {"日期": "2024-01-01", "类别": "营收", "数值": 1},
        {"日期": "2024-01-02", "类别": "营收", "数值": 2},
        {"日期": "2024-01-01", "类别": "成本", "数值": 1},
    ]
    assert summarize_by_category(data) == {"营收": 3, "成本": 1}
'''


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _make_repo() -> str:
    d = tempfile.mkdtemp(prefix="testrepo-")
    (Path(d) / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    (Path(d) / "src" / "reports").mkdir(parents=True)
    (Path(d) / "src" / "reports" / "__init__.py").write_text("")
    (Path(d) / "src" / "reports" / "summary.py").write_text(SUMMARY_SRC)
    (Path(d) / "tests").mkdir()
    (Path(d) / "tests" / "test_summary_baseline.py").write_text(BASELINE_TEST)
    _git(d, "init")
    _git(d, "add", "-A")
    _git(d, "commit", "-m", "base")
    return d


def _pass_tests_json() -> str:
    return json.dumps(
        {
            "files": {
                "tests/test_summary_unit.py": (
                    "from reports.summary import summarize_by_category\n\n"
                    "def test_empty():\n    assert summarize_by_category([]) == {}\n\n"
                    "def test_single():\n"
                    "    assert summarize_by_category([{'类别': '营收', '数值': 5}]) == {'营收': 5}\n"
                ),
                "tests/test_summary_integration.py": (
                    "from reports.summary import summarize_by_category\n\n"
                    "def test_multi_rows_chain():\n"
                    "    rows = [{'类别': c, '数值': v} for c, v in [('营收', 1), ('营收', 2), ('成本', 4)]]\n"
                    "    assert summarize_by_category(rows) == {'营收': 3, '成本': 4}\n"
                ),
            }
        },
        ensure_ascii=False,
    )


def _fail_tests_json() -> str:
    """独立用例发现缺陷：空输入应返回 {'empty': True}（现有实现不满足）。"""
    return json.dumps(
        {
            "files": {
                "tests/test_summary_unit.py": (
                    "from reports.summary import summarize_by_category\n\n"
                    "def test_empty_returns_marker():\n"
                    "    assert summarize_by_category([]) == {'empty': True}\n"
                ),
            }
        },
        ensure_ascii=False,
    )


def _fix_json() -> str:
    """AI 修复：空输入返回 {'empty': True}，同时保持既有行为。"""
    fixed = (
        "def summarize_by_category(data):\n"
        "    if not data:\n"
        "        return {'empty': True}\n"
        "    out = {}\n"
        "    for row in data:\n"
        "        out[row['类别']] = out.get(row['类别'], 0) + row['数值']\n"
        "    return out\n"
    )
    return json.dumps({"files": {"src/reports/summary.py": fixed}}, ensure_ascii=False)


def _env(answers: list[str]):
    llm = MockLLM(answers)
    approval = MockApprovalAPI()
    gitlab = MockGitLabAPI()
    repo = _make_repo()
    coder = Coder(llm=llm, gitlab_url="https://gitlab.com", gitlab_token="tok", project_path="sandbox/prodesgin", workdir=repo, push=False)
    tester = TestEngine(llm=llm, coder=coder, gitlab_api=gitlab, gitlab_url="https://gitlab.com", gitlab_token="tok", project_path="sandbox/prodesgin", push=False, workdir=repo)
    app = create_app(approval_api=approval, gitlab_api=gitlab, llm=llm, coder=coder, tester=tester)
    return {"client": TestClient(app), "approval": approval, "gitlab": gitlab, "repo": repo}


def _start(env, task_ref="rel-1"):
    return env["client"].post(
        "/runs",
        json={
            "sop": "test-domain/release-gate",
            "requirement_text": "按类别汇总报表",
            "title": "按类别汇总报表",
            "task_ref": task_ref,
            "project_id": "sandbox/prodesgin",
            "prd_doc": "## 验收标准\n- WHEN 传入多条报表数据 THEN summarize_by_category 按类别聚合数值并返回字典\n- WHEN 数据为空 THEN 返回空字典\n- 数值保留两位小数\n",
        },
    )


class TestTestFlow:
    def test_happy_path_approve(self):
        env = _env(["测试计划：单元+集成分层，覆盖验收标准。", _pass_tests_json(), "测试报告：全部通过，建议放行。"])
        r = _start(env)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g4"
        assert body["approval_id"].startswith("apv_mock")
        assert "passed" in body["test_result"]
        assert body["defects"] == []

        # G4 审批请求上下文含测试报告要素
        payload = env["approval"].requests[-1]
        assert payload["type"] == "GATE_RELEASE_DECISION"
        assert payload["context_snapshot"]["diff_summary"].startswith("独立测试执行通过")

        r = env["client"].post(f"/runs/rel-1/resume", json={"decision": "approve", "comment": "放行"})
        body = r.json()
        assert body["status"] == "done"
        assert env["gitlab"].created == []  # 无缺陷闭环

    def test_defect_loop_fix_and_close(self):
        """独立用例发现缺陷 → 缺陷单 → AI 修复 → 回归通过 → 缺陷单关闭。"""
        env = _env(["测试计划：发现空输入边界缺陷。", _fail_tests_json(), _fix_json(), "测试报告：缺陷已闭环，建议放行。"])
        r = _start(env, task_ref="rel-2")
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g4"
        assert "passed" in body["test_result"]  # 修复后回归通过
        assert len(body["defects"]) == 1
        defect = body["defects"][0]
        assert defect["issue_iid"] == 12 and defect["fixed"] is True and defect["closed"] is True
        assert env["gitlab"].created[0]["labels"] == ["defect", "auto-test"]
        assert 12 in env["gitlab"].closed_issues
        assert (Path(env["repo"]) / "src/reports/summary.py").read_text().startswith("def summarize_by_category")

    def test_g4_reject(self):
        env = _env(["测试计划：分层覆盖。", _pass_tests_json(), "测试报告：通过，建议放行。"])
        r = _start(env, task_ref="rel-3")
        tid = r.json()["thread_id"]
        r = env["client"].post(f"/runs/{tid}/resume", json={"decision": "reject", "comment": "覆盖不足，补充异常场景"})
        body = r.json()
        assert body["status"] == "rejected"
        assert body["comment"] == "覆盖不足，补充异常场景"

    def test_state_endpoint_g4(self):
        env = _env(["测试计划：分层覆盖。", _pass_tests_json(), "测试报告：通过，建议放行。"])
        r = _start(env, task_ref="rel-4")
        tid = r.json()["thread_id"]
        state = env["client"].get(f"/runs/{tid}/state").json()
        assert "gate_g4" in state["next"]
        assert state["interrupts"][0]["gate"] == "G4_RELEASE_DECISION"

    def test_gen_failure_no_defect(self):
        """用例生成收集错误（LLM 幻觉 import）→ 如实上报，不误建缺陷单。"""
        broken = json.dumps(
            {"files": {"tests/test_ghost_unit.py": "from src.reports.ghost import Ghost\n"}},
            ensure_ascii=False,
        )
        env = _env(["测试计划：分层覆盖。", broken, broken, broken, "测试报告：用例生成失败。"])
        r = _start(env, task_ref="rel-5")
        body = r.json()
        assert body["status"] == "waiting_approval" and body["gate"] == "gate_g4"
        assert "用例生成失败" in body["test_result"]
        assert body["defects"] == []  # 不误建缺陷单
        assert env["gitlab"].created == []
