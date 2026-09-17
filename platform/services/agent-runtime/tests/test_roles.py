"""WP6 岗位注册表测试：三件套校验、边界、技能-工具绑定、指纹、提示词渲染。

对应课程图 55–57、59：角色边界要写到具体范围；没有挂载的能力不能宣称拥有；
公司规则由权威侧维护；"看不见"与"不能改"是两套机制。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from app.roles import RoleError, load_all, load_role, roles_dir, summary

ROOT = Path(__file__).resolve().parents[3]
REAL_ROLES = ROOT / "roles"


@pytest.fixture()
def roles_copy(tmp_path) -> Path:
    dst = tmp_path / "roles"
    shutil.copytree(REAL_ROLES, dst)
    return dst


class TestRealRoles:
    def test_three_roles_load(self):
        roles = load_all(REAL_ROLES)
        assert {r.name for r in roles} == {"ops-duty", "dev-agent", "product-agent"}
        assert all(r.enabled for r in roles)

    def test_every_role_has_boundaries_and_owner(self):
        for role in load_all(REAL_ROLES):
            assert role.meta.get("scope"), f"{role.name} 缺 scope"
            assert role.meta.get("out_of_scope"), f"{role.name} 缺 out_of_scope"
            assert role.meta.get("owner"), f"{role.name} 缺 owner"

    def test_every_skill_binds_a_tool(self):
        for role in load_all(REAL_ROLES):
            for skill in role.skills:
                assert skill.get("tool"), f"{role.name}/{skill.get('id')} 未绑定工具"

    def test_every_kb_prefix_has_authority(self):
        for role in load_all(REAL_ROLES):
            for src in role.kb.get("sources", []):
                assert src.get("authority"), f"{role.name} 的知识来源 {src.get('prefix')} 无权威侧"

    def test_fingerprint_is_stable_and_distinct(self):
        first = {r.name: r.fingerprint() for r in load_all(REAL_ROLES)}
        second = {r.name: r.fingerprint() for r in load_all(REAL_ROLES)}
        assert first == second
        assert len(set(first.values())) == len(first), "不同岗位指纹不应相同"

    def test_summary_shape(self):
        info = summary(REAL_ROLES)
        assert info["count"] == 3 and info["errors"] == []
        ops = next(r for r in info["roles"] if r["role"] == "ops-duty")
        assert ops["tools"] and ops["kb_prefixes"] and ops["fingerprint"]


class TestValidationRejects:
    def test_missing_out_of_scope_rejected(self, roles_copy):
        path = roles_copy / "ops-duty" / "agent.md"
        meta, body = path.read_text(encoding="utf-8").split("---", 2)[1], ""
        doc = yaml.safe_load(meta)
        doc.pop("out_of_scope")
        path.write_text("---\n" + yaml.safe_dump(doc, allow_unicode=True) + "---\n\n正文\n", encoding="utf-8")
        with pytest.raises(RoleError, match="out_of_scope"):
            load_role("ops-duty", roles_copy)

    def test_missing_owner_rejected(self, roles_copy):
        path = roles_copy / "dev-agent" / "agent.md"
        doc = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
        doc.pop("owner")
        path.write_text("---\n" + yaml.safe_dump(doc, allow_unicode=True) + "---\n\n正文\n", encoding="utf-8")
        with pytest.raises(RoleError, match="owner"):
            load_role("dev-agent", roles_copy)

    def test_missing_agent_md_rejected(self, roles_copy):
        (roles_copy / "product-agent" / "agent.md").unlink()
        with pytest.raises(RoleError, match="缺少 agent.md"):
            load_role("product-agent", roles_copy)

    def test_missing_skills_rejected(self, roles_copy):
        (roles_copy / "ops-duty" / "skills.yaml").unlink()
        with pytest.raises(RoleError, match="缺少 skills.yaml"):
            load_role("ops-duty", roles_copy)

    def test_skill_without_tool_rejected(self, roles_copy):
        path = roles_copy / "ops-duty" / "skills.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["skills"].append({"id": "magic", "description": "口头宣称的能力"})
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(RoleError, match="未挂载的能力"):
            load_role("ops-duty", roles_copy)

    def test_unknown_tool_rejected_when_registry_provided(self, roles_copy):
        """工具未在注册表登记 → 拒绝加载（执行期鉴权的前提）。"""
        with pytest.raises(RoleError, match="未注册的工具"):
            load_role("ops-duty", roles_copy, known_tools={"kb.search"})

    def test_duplicate_skill_id_rejected(self, roles_copy):
        path = roles_copy / "dev-agent" / "skills.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["skills"].append(dict(doc["skills"][0]))
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(RoleError, match="技能 id 重复"):
            load_role("dev-agent", roles_copy)

    def test_kb_without_authority_rejected(self, roles_copy):
        path = roles_copy / "product-agent" / "kb.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["sources"][0].pop("authority")
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(RoleError, match="authority"):
            load_role("product-agent", roles_copy)

    def test_summary_reports_errors_instead_of_crashing(self, roles_copy):
        (roles_copy / "ops-duty" / "kb.yaml").unlink()
        info = summary(roles_copy)
        assert info["count"] == 2 and len(info["errors"]) == 1
        assert "ops-duty" in info["errors"][0]


class TestPromptExcerpt:
    def test_excerpt_contains_scope_boundaries_and_rules(self):
        role = load_role("ops-duty", REAL_ROLES)
        text = role.prompt_excerpt()
        assert "不负责" in text and "越界即交回人" in text
        assert "未挂载的能力不得使用" in text
        assert "判断标准" in text and "权威侧维护" in text
        assert role.fingerprint() in text

    def test_excerpt_changes_when_scope_changes(self, roles_copy):
        before = load_role("ops-duty", roles_copy).prompt_excerpt()
        path = roles_copy / "ops-duty" / "agent.md"
        text = path.read_text(encoding="utf-8").replace('environments: ["prod", "staging"]',
                                                        'environments: ["prod"]')
        path.write_text(text, encoding="utf-8")
        after = load_role("ops-duty", roles_copy).prompt_excerpt()
        assert before != after

    def test_roles_dir_env_override(self, monkeypatch, roles_copy):
        monkeypatch.setenv("ROLES_DIR", str(roles_copy))
        assert roles_dir() == roles_copy
        assert len(load_all()) == 3


class TestRoleWiredIntoOpsFlow:
    """岗位不能只是"放在仓库里的配置"——必须真的进入 Agent 的 system prompt。"""

    def test_ops_diagnose_prompt_contains_role_boundaries(self):
        from app.apis import MockApprovalAPI, MockGitLabAPI
        from app.llm import MockLLM
        from app.main import create_app
        from fastapi.testclient import TestClient

        captured: list[str] = []

        class CapturingLLM(MockLLM):
            def chat(self, messages):
                captured.append("\n".join(m.get("content", "") for m in messages))
                # 诊断节点期望 JSON
                return ('{"root_cause": "连接池耗尽", "impact": "下单失败", "change_type": "operational", '
                        '"change_summary": "扩容连接池", "risk_level": "L2"}')

        app = create_app(approval_api=MockApprovalAPI(), gitlab_api=MockGitLabAPI(), llm=CapturingLLM(answers=[]))
        client = TestClient(app)
        r = client.post("/runs", json={
            "sop": "ops-domain/alert-to-postmortem",
            "requirement_text": "order-api 5xx 升高",
            "alert": {"title": "HighErrorRate", "message": "order-api 5xx 12%"},
            "context": "hikari pool timeout",
            "runbook": "扩容连接池",
        })
        assert r.status_code == 200, r.text
        assert captured, "诊断节点应调用 LLM"
        system = captured[0]
        assert "岗位" in system and "不负责" in system, "岗位边界必须进入 system prompt"
        assert "未挂载的能力不得使用" in system
