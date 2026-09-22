"""并发/竞态专项回归（专项 CONC-001）：一次提交版本只能落一条决策。

背景（实测，不是推测）：PG 后端下 30 并发对同一审批单提交决策，8 轮里 6 轮插入了 2–5 条
决策行、5 个请求都返回 200 —— 一次审批被"批了多次"，审计留痕与下游 resume 都会被重复触发。
根因是"读状态 → 判 pending → 写决策"在 READ COMMITTED 下没有互斥。

三道防线（本文件各测一道）：
  ① `SELECT … FOR UPDATE` 行锁（在 PG 方言下真的发出 FOR UPDATE）；
  ② 数据库唯一键 `uq_decision_request_version`（确定性可测：预置同版本决策行 → 409）；
  ③ 应用层 `_check_pending`（既有测试已覆盖 409/410）。

SQLite 无行锁、单写者，因此这里不断言"多线程下只成功一次"（那在 SQLite 上测不出真实语义，
会给出虚假的安全感）；真实并发验证走 `test-campaign/probes-concurrency.sh decide-race-stress`
打 PG 后端。这里断言的是**结构保证**与**唯一键兜底**，两者在两种数据库上都必须成立。
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

_tmpdir = tempfile.mkdtemp(prefix="conc-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApprovalRequest, Decision, utcnow  # noqa: E402
from app.router import _get_or_404  # noqa: E402

BODY = {
    "type": "GATE_REQUIREMENT_REVIEW", "stage": "conc", "requester": "campaign",
    "context_snapshot": {"artifact_ref": "a", "diff_summary": "d", "evidence": ["e"],
                         "risk": {"score": 10, "level": "L2", "factors": []}},
    "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _new_approval(client, ref: str) -> dict:
    payload = dict(BODY, task_ref=ref, deadline=(utcnow() + timedelta(hours=2)).isoformat())
    r = client.post("/approvals", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestDecisionUniqueness:
    """① 行锁确实存在（结构保证，不依赖运气）。"""

    def test_decision_path_uses_row_lock(self):
        with SessionLocal() as db:
            stmt = select(ApprovalRequest).where(ApprovalRequest.id == "apv_x").with_for_update()
            sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql.upper(), sql

    def test_get_or_404_forwards_for_update_flag(self):
        """`_get_or_404` 是决策/重提/超时三条写路径的唯一入口，必须能把锁带下去。"""
        import inspect

        src = inspect.getsource(_get_or_404)
        assert "with_for_update" in src
        assert "for_update" in inspect.signature(_get_or_404).parameters

    def test_write_paths_all_request_the_lock(self):
        """decide / resubmit / im callback / sweeper 四条写路径都不得漏掉锁。"""
        from app import router as r
        import inspect

        for fn in (r.decide, r.resubmit, r.im_card_callback):
            assert "for_update=True" in inspect.getsource(fn), fn.__name__
        from app import sweeper

        assert "with_for_update" in inspect.getsource(sweeper.sweep)

    def test_unique_key_declared_on_metadata(self):
        keys = {c.name for c in Decision.__table__.constraints if hasattr(c, "name")}
        assert "uq_decision_request_version" in keys, keys

    def test_sweeper_records_version(self):
        """超时处置也写 version，否则超时决策会被唯一键误判为重复。"""
        import inspect

        from app import sweeper

        assert "version=req.version" in inspect.getsource(sweeper.sweep)


class TestUniquenessBackstop:
    """② 唯一键兜底：即使有人绕过行锁（多进程/直连库），同版本决策只能有一条。"""

    def test_duplicate_decision_for_same_version_is_rejected(self, client):
        apv = _new_approval(client, "conc-dup-1")
        with SessionLocal() as db:  # 模拟"另一个进程已经批过这一版"
            db.add(Decision(request_id=apv["id"], version=1, decision="approve",
                            decided_by="other-process", comment="先到的决策"))
            db.commit()
        r = client.post(f"/approvals/{apv['id']}/decision",
                        json={"decision": "approve", "decided_by": "racer", "comment": "后到的决策"})
        assert r.status_code == 409, r.text
        with SessionLocal() as db:
            rows = db.scalars(select(Decision).where(Decision.request_id == apv["id"])).all()
        assert len(rows) == 1, [d.decided_by for d in rows]

    def test_decision_carries_the_submitted_version(self, client):
        """revise → 重新提交（version+1）后可以再决策一次：唯一键按版本放行，不误伤正常流程。"""
        apv = _new_approval(client, "conc-version-2")
        r1 = client.post(f"/approvals/{apv['id']}/decision",
                         json={"decision": "revise", "decided_by": "评审员", "comment": "改一下"})
        assert r1.status_code == 200, r1.text
        assert r1.json()["version"] == 1

        body = dict(BODY, task_ref="conc-version-2",
                    deadline=(utcnow() + timedelta(hours=2)).isoformat())
        r2 = client.post(f"/approvals/{apv['id']}/resubmit", json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json()["version"] == 2

        r3 = client.post(f"/approvals/{apv['id']}/decision",
                         json={"decision": "approve", "decided_by": "评审员", "comment": "通过"})
        assert r3.status_code == 200, r3.text
        assert r3.json()["version"] == 2          # 决策记得住"批的是哪一版"
        with SessionLocal() as db:
            rows = db.scalars(select(Decision).where(Decision.request_id == apv["id"])).all()
        assert sorted(d.version for d in rows) == [1, 2]

    def test_second_decision_on_same_version_still_409(self, client):
        apv = _new_approval(client, "conc-dup-2")
        assert client.post(f"/approvals/{apv['id']}/decision",
                           json={"decision": "approve", "decided_by": "a", "comment": "c"}).status_code == 200
        r = client.post(f"/approvals/{apv['id']}/decision",
                        json={"decision": "reject", "decided_by": "b", "comment": "c"})
        assert r.status_code == 409
        with SessionLocal() as db:
            assert len(db.scalars(select(Decision).where(Decision.request_id == apv["id"])).all()) == 1
