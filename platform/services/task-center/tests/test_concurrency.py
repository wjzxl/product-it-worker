"""并发/竞态专项回归（专项 CONC-002）：任务状态流转必须串行化。

背景（实测）：PG 后端下 30 并发对同一任务提交 `waiting_approval → running`，6 轮里 3 轮
有 2–15 个请求返回 200、时间线多出 1–14 条 `status_changed`。根因与审批决策同源：
"读状态 → 判合法性 → 写新状态"没有行锁，并发请求各自读到旧状态。

SQLite 单写者，多线程测试测不出真实语义，因此这里断言**结构保证**
（流转路径确实请求了行锁），真实并发验证走
`test-campaign/probes-concurrency.sh task-race-stress`（30 并发 × 8 轮，直接对账数据库行数）。
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

_tmp = tempfile.mkdtemp(prefix="conc-task-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/tc.db"

from app import router as tc_router  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Task  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestTransitionSerialisation:
    def test_transition_uses_row_lock(self):
        stmt = select(Task).where(Task.id == "tsk_x").with_for_update()
        assert "FOR UPDATE" in str(stmt.compile(dialect=postgresql.dialect())).upper()

    def test_transition_path_forwards_for_update(self):
        src = inspect.getsource(tc_router.transition)
        assert "for_update=True" in src, src

    def test_get_or_404_supports_lock(self):
        params = inspect.signature(tc_router._get_or_404).parameters
        assert "for_update" in params
        assert "with_for_update" in inspect.getsource(tc_router._get_or_404)

    def test_second_transition_after_state_change_is_rejected(self, client):
        """语义不变：状态已变后重复流转仍是 409（锁只是让这判断不被并发绕过）。"""
        t = client.post("/tasks", json={"sop": "dev-domain/issue-to-merge"},
                        headers={"X-Tenant": "conc"}).json()
        client.post(f"/tasks/{t['id']}/transition", json={"to": "running", "by": "agent"},
                    headers={"X-Tenant": "conc"})
        client.post(f"/tasks/{t['id']}/transition", json={"to": "waiting_approval", "by": "agent"},
                    headers={"X-Tenant": "conc"})
        body = {"to": "running", "by": "h", "detail": {"approval_id": "apv_x", "decided_by": "r"}}
        assert client.post(f"/tasks/{t['id']}/transition", json=body,
                           headers={"X-Tenant": "conc"}).status_code == 200
        r = client.post(f"/tasks/{t['id']}/transition", json=body, headers={"X-Tenant": "conc"})
        assert r.status_code == 409, r.text
        events = client.get(f"/tasks/{t['id']}",
                            headers={"X-Tenant": "conc"}).json().get("events", [])
        changed = [e for e in events if e.get("event_type") == "status_changed"]
        assert len(changed) == 3, [e.get("detail") for e in changed]
