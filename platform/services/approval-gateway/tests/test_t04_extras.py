"""T0.4 收尾测试：超时扫描器 + 飞书卡片 + Web 审批台。"""
import logging
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models import ApprovalRequest, utcnow
from app.notifier import FeishuNotifier, build_card

from test_api import _payload  # 复用基础载荷构造


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


# 避免循环导入：直接在用例内取 app
import app.main as app_module  # noqa: E402


def _make_request(**overrides) -> ApprovalRequest:
    """构造内存中的审批请求对象（notifier 测试用，不落库）。"""
    p = _payload()
    p.update(overrides)
    return ApprovalRequest(
        id="apv_test01",
        type=p["type"],
        stage=p["stage"],
        requester=p["requester"],
        task_ref=p.get("task_ref", ""),
        context_snapshot=p["context_snapshot"],
        options=p["options"],
        deadline=utcnow() + timedelta(hours=24),
        escalation_policy=p["escalation_policy"],
        status="pending",
        version=1,
    )


class TestSweeper:
    def test_overdue_default_reject(self, client):
        p = _payload(deadline=(utcnow() - timedelta(hours=1)).isoformat())
        apv = client.post("/approvals", json=p).json()

        r = client.post("/admin/sweep")
        assert r.status_code == 200
        assert r.json()["handled"] >= 1  # 可能一并处置了其他测试遗留的超时单

        apv = client.get(f"/approvals/{apv['id']}").json()
        assert apv["status"] == "rejected"
        assert apv["decisions"][-1]["decided_by"] == "system:timeout"
        assert "超时未处理" in apv["decisions"][-1]["comment"]

    def test_l3_never_auto_approve(self, client):
        p = _payload(
            deadline=(utcnow() - timedelta(minutes=5)).isoformat(),
            escalation_policy={"remind_after_hours": 1, "escalate_to": "x", "timeout_action": "auto_approve_l1_only"},
        )
        p["context_snapshot"]["risk"]["level"] = "L3"  # L3 强制拒绝，无视策略
        apv = client.post("/approvals", json=p).json()
        client.post("/admin/sweep")
        assert client.get(f"/approvals/{apv['id']}").json()["status"] == "rejected"

    def test_l1_auto_approve_requires_server_side_trust(self, client):
        """BUG-004 修复后的语义：超时自动放行只认**服务端信任等级**，不认单据自述等级。

        未积累信任（服务端 L3）时，即使单据自述 level=L1 且策略声明 auto_approve_l1_only，
        也必须拒绝——否则提交方自封 L1 就能换取自动放行。
        """
        p = _payload(
            deadline=(utcnow() - timedelta(minutes=5)).isoformat(),
            escalation_policy={"remind_after_hours": 1, "escalate_to": "x", "timeout_action": "auto_approve_l1_only"},
        )
        p["context_snapshot"]["risk"]["level"] = "L1"
        apv = client.post("/approvals", json=p).json()
        client.post("/admin/sweep")
        assert client.get(f"/approvals/{apv['id']}").json()["status"] == "rejected"
        detail = client.get(f"/approvals/{apv['id']}").json()["decisions"][-1]["comment"]
        assert "服务端信任等级" in detail and "不作为依据" in detail

    def test_l1_auto_approve_honored_when_trust_is_l1(self, client):
        """真正达到服务端 L1 信任时，策略声明的自动放行仍然生效（安全不等于永远拒绝）。"""
        gate = "GATE_RELEASE_DECISION"
        base = _payload()
        for _ in range(12):                       # 积累信任：连续 12 次人工批准 → L1
            created = client.post("/approvals", json={**base, "type": gate}).json()
            client.post(f"/approvals/{created['id']}/decision",
                        json={"decision": "approve", "decided_by": "信任积累"})
        assert client.get(f"/trust/{gate}").json()["level"] == "L1"

        p = _payload(
            type=gate,
            deadline=(utcnow() - timedelta(minutes=5)).isoformat(),
            escalation_policy={"remind_after_hours": 1, "escalate_to": "x",
                               "timeout_action": "auto_approve_l1_only"},
        )
        apv = client.post("/approvals", json=p).json()
        client.post("/admin/sweep")
        assert client.get(f"/approvals/{apv['id']}").json()["status"] == "approved"

    def test_not_overdue_untouched(self, client):
        p = _payload(deadline=(utcnow() + timedelta(hours=5)).isoformat())
        apv = client.post("/approvals", json=p).json()
        client.post("/admin/sweep")
        assert client.get(f"/approvals/{apv['id']}").json()["status"] == "pending"


class TestFeishuCard:
    def test_build_card_structure(self):
        req = _make_request()
        card = build_card(req, "https://approve.local")
        assert card["header"]["title"]["content"] == "[待审批] 产品域 G1"
        content = str(card["elements"])
        assert "报表导出功能" in content          # 做了什么
        assert "prd-v3.md" in content             # 依据
        assert "L2" in content                    # 风险自评
        assert "approval_id=apv_test01" in content  # 跳转审批台

    def test_feishu_send_payload(self):
        import json

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.read().decode())
            return httpx.Response(200, json={"StatusCode": 0})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        notifier = FeishuNotifier("https://open.feishu.cn/open-apis/bot/hook/x", client=client)
        result = notifier.send(_make_request())
        assert result == "apv_test01"
        assert captured["json"]["msg_type"] == "interactive"
        assert "产品域 G1" in captured["json"]["card"]["header"]["title"]["content"]

    def test_feishu_send_failure_swallowed(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        notifier = FeishuNotifier("https://open.feishu.cn/open-apis/bot/hook/x", client=client)
        caplog.set_level(logging.ERROR)
        assert notifier.send(_make_request()) == ""  # 推送失败不阻断审批流程
        assert "推送失败" in caplog.text

    def test_feishu_rejected_body_returns_empty(self, caplog):
        # 飞书业务错误也返回 HTTP 200，需校验响应体 code
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 19021, "msg": "keyword not found"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        notifier = FeishuNotifier("https://open.feishu.cn/open-apis/bot/hook/x", client=client)
        caplog.set_level(logging.ERROR)
        assert notifier.send(_make_request()) == ""
        assert "推送被拒" in caplog.text


class TestWebConsole:
    def test_console_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "审批台" in r.text
