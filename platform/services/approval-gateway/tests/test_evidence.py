"""WP5 证据库与验收绑定测试。

验收口径（17 号计划 WP5）：
  ① 上传/下载/hash 校验；
  ② 每条证据必须绑定验收项（否则无法用于交付判定）；
  ③ 非法类型/超大/空内容被拒绝；
  ④ 证据丢失或损坏时复验报错（不能"看起来有证据"）；
  ⑤ E3 的 acceptance_evidence **从证据库自动绑定**，不依赖调用方自报；
  ⑥ 租户隔离。
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="wp5-evidence-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["EVIDENCE_DIR"] = f"{_tmpdir}/evidence"

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _upload(client, task_ref: str, item: str, kind: str = "export", content: bytes = b"a,b\n1,2\n",
            filename: str = "export-sample.csv", tenant: str = "default"):
    return client.post(
        "/evidence",
        data={"task_ref": task_ref, "acceptance_item": item, "kind": kind, "note": "演练证据"},
        files={"file": (filename, content, "text/csv")},
        headers={"X-Tenant": tenant},
    )


class TestUploadAndFetch:
    def test_upload_and_download_roundtrip(self, client):
        r = _upload(client, "wp5-a", "acc-1")
        assert r.status_code == 201, r.text
        meta = r.json()
        assert meta["sha256"] and meta["size"] == 8
        assert meta["acceptance_item"] == "acc-1" and meta["kind"] == "export"

        content = client.get(f"/evidence/{meta['id']}/content")
        assert content.status_code == 200
        assert content.content == b"a,b\n1,2\n"
        assert content.headers["X-Evidence-Sha256"] == meta["sha256"]

    def test_list_by_task_groups_by_acceptance_item(self, client):
        _upload(client, "wp5-b", "acc-1")
        _upload(client, "wp5-b", "acc-1", kind="log", filename="run.log", content=b"log line")
        _upload(client, "wp5-b", "acc-2", kind="screenshot", filename="empty.png", content=b"\x89PNG")
        body = client.get("/evidence", params={"task_ref": "wp5-b"}).json()
        assert len(body["evidence"]) == 3
        # by_acceptance_item 是"验收项 → 证据 id 列表"（E3 闸门据此判定覆盖）
        assert set(body["by_acceptance_item"]) == {"acc-1", "acc-2"}
        assert len(body["by_acceptance_item"]["acc-1"]) == 2
        assert len(body["by_acceptance_item"]["acc-2"]) == 1
        assert all(eid.startswith("evd_") for ids in body["by_acceptance_item"].values() for eid in ids)

    def test_verify_reports_all_valid(self, client):
        _upload(client, "wp5-c", "acc-1")
        body = client.get("/evidence/verify", params={"task_ref": "wp5-c"}).json()
        assert body["total"] == 1 and body["verified"] == 1 and body["broken"] == []


class TestRejections:
    def test_missing_acceptance_item_rejected(self, client):
        r = client.post("/evidence", data={"task_ref": "wp5-x", "kind": "log"},
                        files={"file": ("a.log", b"x", "text/plain")})
        assert r.status_code == 422  # 表单必填项缺失

    def test_bad_kind_rejected(self, client):
        r = _upload(client, "wp5-x", "acc-1", kind="随便")
        assert r.status_code == 400 and "证据类型" in r.json()["detail"]

    def test_bad_suffix_rejected(self, client):
        r = _upload(client, "wp5-x", "acc-1", filename="malware.exe", content=b"MZ")
        assert r.status_code == 400 and "不支持的文件类型" in r.json()["detail"]

    def test_empty_content_rejected(self, client):
        r = _upload(client, "wp5-x", "acc-1", content=b"")
        assert r.status_code == 400 and "为空" in r.json()["detail"]

    def test_unknown_artifact_404(self, client):
        assert client.get("/evidence/evd_nope/content").status_code == 404


class TestIntegrity:
    def test_verify_detects_tampering(self, client, tmp_path):
        """证据被改动或丢失时，复验必须报错（不能"看起来有证据"）。"""
        meta = _upload(client, "wp5-tamper", "acc-1").json()
        path = None
        for root, _dirs, files in os.walk(os.environ["EVIDENCE_DIR"]):
            for name in files:
                if meta["id"] in name:
                    path = os.path.join(root, name)
        assert path, "应能在证据目录找到工件"
        os.remove(path)
        body = client.get("/evidence/verify", params={"task_ref": "wp5-tamper"}).json()
        assert body["verified"] == 0 and len(body["broken"]) == 1
        assert "丢失" in body["broken"][0]["error"]
        # 下载也应明确报错，而不是返回空内容
        assert client.get(f"/evidence/{meta['id']}/content").status_code == 409

    def test_tenant_isolation(self, client):
        meta = _upload(client, "wp5-tenant", "acc-1", tenant="line-a").json()
        assert client.get(f"/evidence/{meta['id']}/content", headers={"X-Tenant": "line-b"}).status_code == 404
        assert client.get("/evidence", params={"task_ref": "wp5-tenant"}, headers={"X-Tenant": "line-b"}).json()["evidence"] == []


class TestE3Binding:
    """E3 的 acceptance_evidence 必须从证据库自动绑定（WP3 里是占位，这里接真）。"""

    DESIGN = {"report": {f: {"x": "y"} for f in ("data", "business", "api", "frontend", "test")}}
    UX = {"empty": "空态", "error": "报错", "loading": "加载"}

    def _pass_e1_e2(self, client, task_ref: str):
        e1 = client.post("/eng-gates/E1/check", json={"task_ref": task_ref, "context": {
            "prd_ref": "PRD v1", "sources": [{"kind": "chat", "ref": "feishu://x", "quote": "导出"}],
            "change_targets": ["src/a.py"], "impact_scope": "仅新增", "rollback_plan": "切回"}}).json()
        e2 = client.post("/eng-gates/E2/check", json={"task_ref": task_ref, "context": {
            "design_docs": self.DESIGN, "ux_states": self.UX,
            "api_contract_ref": "a", "data_dictionary_ref": "b"}}).json()
        assert e1["passed"] and e2["passed"]

    def _e3(self, client, task_ref: str, items: list[dict]):
        return client.post("/eng-gates/E3/check", json={"task_ref": task_ref, "context": {
            "artifacts": ["src/a.py"], "acceptance_items": items,
            "plan_consistency": "一致", "handover": "README"}}).json()

    def test_missing_evidence_blocks_then_upload_passes(self, client):
        task = "wp5-e3"
        self._pass_e1_e2(client, task)
        items = [{"id": "acc-1"}, {"id": "acc-2"}]

        before = self._e3(client, task, items)
        assert before["blocked"] is True
        detail = next(g["detail"] for g in before["gaps"] if g["id"] == "acceptance_evidence")
        assert "acc-1" in detail and "acc-2" in detail

        # 只补一条：仍缺 acc-2（不能因为"传了一条证据就通过"）
        _upload(client, task, "acc-1")
        partial = self._e3(client, task, items)
        assert partial["blocked"] is True
        assert "acc-2" in next(g["detail"] for g in partial["gaps"] if g["id"] == "acceptance_evidence")

        # 两条齐 → 放行
        _upload(client, task, "acc-2", kind="log", filename="run.log", content=b"ok")
        after = self._e3(client, task, items)
        assert after["passed"] is True, after["gaps"]

    def test_not_applicable_item_still_honored(self, client):
        """显式声明不适用的验收项无需证据（与 E3 的语义一致）。"""
        task = "wp5-e3-na"
        self._pass_e1_e2(client, task)
        items = [{"id": "acc-1", "evidence": ["manual-check"]},
                 {"id": "acc-2", "not_applicable": True, "reason": "该环境无多租户场景"}]
        assert self._e3(client, task, items)["passed"] is True
