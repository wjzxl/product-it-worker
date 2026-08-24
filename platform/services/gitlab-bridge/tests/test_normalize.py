"""归一化测试：GitLab webhook 载荷 → 平台事件。"""
import copy

from app.normalize import normalize

ISSUE_OPEN = {
    "object_kind": "issue",
    "user": {"name": "Alice", "username": "alice"},
    "project": {"id": 42, "path_with_namespace": "sandbox/prodesgin"},
    "object_attributes": {
        "iid": 12,
        "title": "报表导出功能",
        "description": "支持按日期筛选，导出 CSV",
        "state": "opened",
        "action": "open",
        "labels": [{"title": "product"}, {"title": "G1-passed"}],
    },
}

MR_OPEN = {
    "object_kind": "merge_request",
    "project": {"id": 42, "path_with_namespace": "sandbox/prodesgin"},
    "object_attributes": {
        "iid": 45,
        "title": "feat: 报表导出",
        "action": "open",
        "state": "opened",
        "source_branch": "feat/report",
        "target_branch": "main",
    },
}

PIPELINE = {
    "object_kind": "pipeline",
    "project": {"id": 42, "path_with_namespace": "sandbox/prodesgin"},
    "object_attributes": {"id": 99, "status": "success", "ref": "main", "sha": "abc123"},
}


class TestNormalize:
    def test_issue_opened(self):
        events = normalize(ISSUE_OPEN, "uuid-111")
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "gitlab.issue.opened"
        assert ev["source"] == "gitlab-webhook"
        assert ev["trace_id"] == "gl-uuid-111"  # 幂等去重键
        assert ev["payload"]["iid"] == 12
        assert ev["payload"]["labels"] == ["product", "G1-passed"]
        assert ev["event_id"].startswith("evt_")

    def test_issue_updated(self):
        payload = copy.deepcopy(ISSUE_OPEN)
        payload["object_attributes"]["action"] = "update"
        payload["object_attributes"]["state"] = "closed"
        payload["changes"] = {"state": {"previous": "opened", "current": "closed"}}
        events = normalize(payload)
        assert events[0]["event_type"] == "gitlab.issue.updated"
        assert events[0]["payload"]["state"] == "closed"
        assert events[0]["payload"]["changes"]["state"]["current"] == "closed"

    def test_mr_opened(self):
        ev = normalize(MR_OPEN)[0]
        assert ev["event_type"] == "gitlab.mr.opened"
        assert ev["payload"]["source_branch"] == "feat/report"

    def test_mr_merged(self):
        payload = copy.deepcopy(MR_OPEN)
        payload["object_attributes"]["action"] = "merge"
        payload["object_attributes"]["state"] = "merged"
        payload["object_attributes"]["merge_commit_sha"] = "deadbeef"
        ev = normalize(payload)[0]
        assert ev["event_type"] == "gitlab.mr.merged"
        assert ev["payload"]["merge_commit_sha"] == "deadbeef"

    def test_pipeline_terminal(self):
        assert normalize(PIPELINE)[0]["event_type"] == "gitlab.pipeline.finished"

        for status in ("running", "pending", "created"):
            p = copy.deepcopy(PIPELINE)
            p["object_attributes"]["status"] = status
            assert normalize(p) == []  # 非终态不产生事件

    def test_unknown_kind_ignored(self):
        assert normalize({"object_kind": "note", "object_attributes": {}}) == []
        assert normalize({"object_kind": "issue", "object_attributes": {"action": "close"}})[0][
            "event_type"
        ] == "gitlab.issue.updated"
