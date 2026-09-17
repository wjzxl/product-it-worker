"""WP8 工具注册表与执行期鉴权测试。

对应课程图 59「两种边界」：
  - **看不见**：工具不在岗位允许列表 → 执行期拒绝（不是提示词劝阻）；
  - **不能改**：写类危险工具（ops.execute）必须带 G5 审批依据。
以及图 62「工具说明」：何时调用、输入输出、失败怎么办都要登记。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from app.apis import MockApprovalAPI, MockGitLabAPI
from app.llm import MockLLM
from app.main import create_app
from app.tools import ToolDenied, ToolError, ToolRegistry, parse_spec

ROOT = Path(__file__).resolve().parents[3]
REAL_TOOLS = ROOT / "tools"


@pytest.fixture()
def registry() -> ToolRegistry:
    return ToolRegistry(REAL_TOOLS)


@pytest.fixture()
def tools_copy(tmp_path) -> Path:
    dst = tmp_path / "tools"
    shutil.copytree(REAL_TOOLS, dst)
    return dst


class TestRegistryLoading:
    def test_real_registry_loads(self, registry):
        assert len(registry.specs) >= 10
        assert "prometheus.query" in registry.ids and "ops.execute" in registry.ids

    def test_every_tool_documents_itself(self, registry):
        """工具说明必须齐全：描述 + 权限点 + 参数类型 + 失败模式。"""
        for spec in registry.specs.values():
            assert spec.description, f"{spec.id} 缺 description"
            assert spec.permission, f"{spec.id} 缺 permission"
            for name, param in spec.params.items():
                assert param.get("type"), f"{spec.id}.{name} 缺 type"
            raw = yaml.safe_load(Path(spec.source_path).read_text(encoding="utf-8"))
            assert raw.get("failure_modes"), f"{spec.id} 未说明失败模式"

    def test_write_and_gated_classification(self, registry):
        info = registry.summary()
        assert "ops.execute" in info["approval_required_tools"]
        assert "prometheus.query" not in info["approval_required_tools"]
        assert info["gated_tools"] == ["ops.execute"]

    def test_invalid_spec_rejected(self):
        with pytest.raises(ToolError, match="kind 必须是"):
            parse_spec({"id": "x", "kind": "magic", "permission": "p", "description": "d"})
        with pytest.raises(ToolError, match="必须声明 permission"):
            parse_spec({"id": "x", "kind": "api", "description": "d"})
        with pytest.raises(ToolError, match="缺少 type"):
            parse_spec({"id": "x", "kind": "api", "permission": "p", "description": "d",
                        "params": {"a": {"required": True}}})

    def test_duplicate_id_rejected(self, tools_copy):
        (tools_copy / "dup.yaml").write_text(
            (tools_copy / "kb.search.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(ToolError, match="id 重复"):
            ToolRegistry(tools_copy)


class TestBoundaries:
    def test_unregistered_tool_denied(self, registry):
        with pytest.raises(ToolDenied) as exc:
            registry.invoke("nope.tool", {})
        assert exc.value.reason == "unregistered"

    def test_invisible_tool_denied_for_role(self, registry):
        """看不见：ops-duty 不该能碰 gitlab.branch_mr 或 ops.execute。"""
        for tool in ("gitlab.branch_mr", "ops.execute"):
            with pytest.raises(ToolDenied) as exc:
                registry.invoke(tool, {"project": "p", "branch": "b", "title": "t"},
                                role="ops-duty", role_tools=["kb.search", "prometheus.query"])
            assert exc.value.reason == "role_not_allowed"

    def test_missing_required_params_denied(self, registry):
        with pytest.raises(ToolDenied) as exc:
            registry.invoke("prometheus.query", {"service": "order-api"},
                            role="ops-duty", role_tools=["prometheus.query"])
        assert exc.value.reason == "missing_params"
        assert "metric" in str(exc.value)

    def test_write_tool_requires_approval_ref(self, registry):
        """不能改：带审批号才放行。"""
        with pytest.raises(ToolDenied) as exc:
            registry.invoke("ops.execute", {"action": "restart", "target": "order-api"},
                            role="ops-duty", role_tools=["ops.execute"])
        assert exc.value.reason == "approval_required"

        registry.register_handler("ops.execute", lambda action, target, approval_ref, params=None: {"executed": True})
        ok = registry.invoke("ops.execute", {"action": "restart", "target": "order-api", "approval_ref": "apv_1"},
                             role="ops-duty", role_tools=["ops.execute"], approval_ref="apv_1")
        assert ok["status"] == "ok" and ok["result"]["executed"] is True

    def test_read_only_tool_passes_without_approval(self, registry):
        out = registry.invoke("kb.search", {"query": "连接池"}, role="ops-duty", role_tools=["kb.search"])
        assert out["status"] in ("ok", "no_handler")

    def test_denials_are_audited(self, registry):
        with pytest.raises(ToolDenied):
            registry.invoke("ops.execute", {"action": "restart", "target": "x"},
                            role="ops-duty", role_tools=["ops.execute"])
        entry = registry.audit_log[-1]
        assert entry["outcome"] == "denied" and entry["reason"] == "approval_required"

    def test_strict_audit_keeps_params_others_redact(self, registry):
        registry.register_handler("kb.search", lambda query, **kw: {"count": 1})
        registry.invoke("kb.search", {"query": "敏感查询"}, role="ops-duty", role_tools=["kb.search"])
        assert registry.audit_log[-1]["params"] == {"query": "<redacted>"}   # standard 级脱敏

        registry.register_handler("ops.execute", lambda **kw: {"executed": True})
        registry.invoke("ops.execute", {"action": "restart", "target": "order-api", "approval_ref": "apv_9"},
                        role="ops-duty", role_tools=["ops.execute"], approval_ref="apv_9")
        assert registry.audit_log[-1]["params"]["target"] == "order-api"     # strict 级留全参

    def test_allow_ungated_is_explicit(self, registry):
        registry.register_handler("ops.execute", lambda **kw: {"executed": True})
        out = registry.invoke("ops.execute", {"action": "restart", "target": "x"},
                              role="platform", role_tools=["ops.execute"], allow_ungated=True)
        assert out["status"] == "ok"


class TestRoleToolAlignment:
    """岗位三件套引用的工具必须已注册（WP6 校验 + WP8 注册表交叉验证）。"""

    def test_all_role_tools_are_registered(self, registry):
        from app.roles import load_all
        for role in load_all(ROOT / "roles", known_tools=registry.ids):
            assert registry.allowed_for(role.tool_ids) == sorted(role.tool_ids), \
                f"{role.name} 引用了未注册工具：{set(role.tool_ids) - registry.ids}"

    def test_role_cannot_see_dangerous_tools(self, registry):
        from app.roles import load_role
        ops = load_role("ops-duty", ROOT / "roles")
        assert "ops.execute" not in ops.tool_ids, "运维值班助理不应自行执行生产变更"
        assert "ops.execute" in registry.summary()["approval_required_tools"]


class TestApi:
    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient
        app = create_app(approval_api=MockApprovalAPI(), gitlab_api=MockGitLabAPI(), llm=MockLLM(answers=[]))
        with TestClient(app) as c:
            yield c

    def test_tools_endpoint(self, client):
        info = client.get("/tools").json()
        assert info["count"] >= 10 and "ops.execute" in info["approval_required_tools"]

    def test_role_scoped_view(self, client):
        info = client.get("/tools", params={"role": "ops-duty"}).json()
        assert info["role_tools"] and "ops.execute" not in info["role_tools"]
        assert info["denied_for_role"] == []

    def test_invoke_denied_returns_403_with_reason(self, client):
        r = client.post("/tools/invoke", json={"tool": "ops.execute",
                                               "params": {"action": "restart", "target": "order-api"},
                                               "role": "ops-duty"})
        assert r.status_code == 403 and r.json()["detail"]["reason"] == "role_not_allowed"

    def test_invoke_registered_handler_works(self, client):
        r = client.post("/tools/invoke", json={"tool": "ambiguity.detect",
                                               "params": {"text": "支持常用格式，数据量大时优化"},
                                               "role": "product-agent"})
        assert r.status_code == 200
        assert len(r.json()["result"]["findings"]) >= 2

    def test_real_handler_bypassing_approval_is_denied(self, client):
        """即便有实现，危险工具仍先过鉴权。"""
        r = client.post("/tools/invoke", json={"tool": "ops.execute",
                                               "params": {"action": "restart", "target": "order-api"},
                                               "role": "ops-duty", "allow_ungated": True})
        assert r.status_code == 403 and r.json()["detail"]["reason"] == "role_not_allowed"

    def test_audit_endpoint(self, client):
        body = client.get("/tools/audit").json()
        assert body["count"] >= 1 and body["entries"]
