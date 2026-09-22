"""外部服务 HTTP 客户端（可注入 transport，测试用 mock）。"""
from __future__ import annotations

from typing import Any

import logging
import os

import httpx

from .service_auth import outbound_headers


logger = logging.getLogger(__name__)


class ApprovalAPI:
    """审批网关客户端。"""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30, transport=transport,
                                    headers=outbound_headers())

    def create_approval(self, payload: dict) -> dict:
        resp = self._client.post(f"{self.base_url}/approvals", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_approval(self, approval_id: str) -> dict:
        resp = self._client.get(f"{self.base_url}/approvals/{approval_id}")
        resp.raise_for_status()
        return resp.json()


class GateAPI:
    """工程闸门客户端（WP2，E1–E3）。

    设计约定：**闸门服务不可用时按"未阻断"处理但显著告警**——闸门是质量增强，
    不应因自身故障把整条流水线拖停（是否收紧由 ENG_GATES_REQUIRED=1 决定）。
    """

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None, required: bool | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30, transport=transport,
                                    headers=outbound_headers())
        self.required = (
            required if required is not None
            else os.getenv("ENG_GATES_REQUIRED", "0") == "1"
        )

    def check(self, code: str, task_ref: str = "", context: dict | None = None, mode: str | None = None,
              checked_by: str = "") -> dict:
        payload: dict = {"task_ref": task_ref, "context": context or {}, "checked_by": checked_by}
        if mode:
            payload["mode"] = mode
        try:
            resp = self._client.post(f"{self.base_url}/eng-gates/{code}/check", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("工程闸门 %s 检查失败（%s: %s）", code, type(exc).__name__, exc)
            if self.required:
                return {
                    "code": code, "mode": "block", "passed": False, "blocked": True,
                    "gaps": [{"id": "gate_unavailable", "ask": f"{code} 闸门不可用",
                              "severity": "critical", "detail": f"{type(exc).__name__}: {exc}",
                              "hint": "按 ENG_GATES_REQUIRED=1 的约定，闸门不可用视为未通过"}],
                    "results": [], "critical_missing": ["gate_unavailable"],
                    "rule_version": "", "fingerprint": "", "check_id": "",
                }
            return {
                "code": code, "mode": "warn", "passed": True, "blocked": False,
                "gaps": [], "results": [], "critical_missing": [],
                "rule_version": "", "fingerprint": "", "check_id": "",
                "unavailable": f"{type(exc).__name__}: {exc}",
            }

    def history(self, task_ref: str = "", code: str = "") -> list[dict]:
        resp = self._client.get(f"{self.base_url}/eng-gates/checks", params={"task_ref": task_ref, "code": code})
        resp.raise_for_status()
        return resp.json()


class GitLabAPI:
    """GitLab 集成桥客户端（桥接 /gitlab/* 辅助端点）。"""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30, transport=transport,
                                    headers=outbound_headers())

    def create_issue(self, project_id: Any, title: str, description: str, labels: list[str], retries: int = 2) -> dict:
        """建单带 5xx/网络重试（代理抖动环境）。"""
        import time as _time

        last: httpx.HTTPStatusError | None = None
        for attempt in range(retries + 1):
            resp = self._client.post(
                f"{self.base_url}/gitlab/issues",
                json={
                    "project_id": project_id,
                    "title": title,
                    "description": description,
                    "labels": labels,
                },
            )
            if resp.status_code < 400:
                return resp.json()
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last = exc
            if attempt < retries:
                _time.sleep(5)
        assert last is not None
        raise last

    def close_issue(self, project_id: Any, iid: int, retries: int = 2) -> dict:
        import time as _time

        last: httpx.HTTPStatusError | None = None
        for attempt in range(retries + 1):
            resp = self._client.post(f"{self.base_url}/gitlab/issues/{project_id}/{iid}/close")
            if resp.status_code < 400:
                return resp.json()
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last = exc
            if attempt < retries:
                _time.sleep(5)
        assert last is not None
        raise last

    def create_mr(self, project_id: Any, source_branch: str, target_branch: str, title: str, description: str) -> dict:
        resp = self._client.post(
            f"{self.base_url}/gitlab/mrs",
            json={
                "project_id": project_id,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def get_mr(self, project_id: Any, mr_iid: int) -> dict:
        resp = self._client.get(f"{self.base_url}/gitlab/mrs/{project_id}/{mr_iid}")
        resp.raise_for_status()
        return resp.json()

    def trigger_pipeline(self, project_id: Any, ref: str = "main") -> dict:
        resp = self._client.post(
            f"{self.base_url}/gitlab/pipelines/trigger",
            json={"project_id": project_id, "ref": ref},
        )
        resp.raise_for_status()
        return resp.json()

    def get_pipeline(self, project_id: Any, pipeline_id: int) -> dict:
        resp = self._client.get(f"{self.base_url}/gitlab/pipelines/{project_id}/{pipeline_id}")
        resp.raise_for_status()
        return resp.json()

    def get_pipeline_jobs(self, project_id: Any, pipeline_id: int) -> list:
        resp = self._client.get(f"{self.base_url}/gitlab/pipelines/{project_id}/{pipeline_id}/jobs")
        resp.raise_for_status()
        return resp.json()

    def merge_mr(self, project_id: Any, mr_iid: int, sha: str | None = None, retries: int = 2) -> dict:
        """合入 MR；GitLab 偶发 4xx/5xx（含新建后立即合入被拒），重试兜底。"""
        import time as _time

        last: httpx.HTTPStatusError | None = None
        for attempt in range(retries + 1):
            resp = self._client.post(
                f"{self.base_url}/gitlab/mrs/{project_id}/{mr_iid}/merge",
                json={"sha": sha} if sha else {},
            )
            if resp.status_code < 400:
                return resp.json()
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last = exc
            if attempt < retries:
                _time.sleep(10)
        assert last is not None
        raise last


class KnowledgeAPI:
    """知识库检索客户端（Runbook RAG）。"""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30, transport=transport,
                                    headers=outbound_headers())

    def search(self, query: str, limit: int = 3) -> list[dict]:
        resp = self._client.post(f"{self.base_url}/search", json={"query": query, "limit": limit})
        resp.raise_for_status()
        return resp.json()


class MockKnowledgeAPI:
    def __init__(self, hits: list[dict] | None = None):
        self.hits = hits if hits is not None else [
            {
                "text": "导出接口 p95 超过 3s：检查日期范围跨度；>365 天时在 fetch_report_data 增加范围校验",
                "source": "export-performance.md",
                "score": 0.92,
            }
        ]
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 3) -> list[dict]:
        self.queries.append(query)
        return self.hits[:limit]


class MockApprovalAPI:
    def __init__(self):
        self.requests: list[dict] = []
        self._counter = 0

    def create_approval(self, payload: dict) -> dict:
        self._counter += 1
        self.requests.append(payload)
        return {"id": f"apv_mock{self._counter}", **payload}

    def get_approval(self, approval_id: str) -> dict:
        return {"id": approval_id, "status": "pending"}


class MockGitLabAPI:
    def __init__(self):
        self.created: list[dict] = []
        self.created_mrs: list[dict] = []
        self.merged: list[dict] = []
        self.closed_issues: list[int] = []

    def create_issue(self, project_id, title, description, labels) -> dict:
        item = {"project_id": project_id, "title": title, "description": description, "labels": labels, "iid": len(self.created) + 12}
        self.created.append(item)
        return item

    def close_issue(self, project_id, iid) -> dict:
        self.closed_issues.append(iid)
        return {"iid": iid, "state": "closed"}

    # pipeline：默认模拟"成功流水线"
    pipeline_status = "success"

    def trigger_pipeline(self, project_id, ref="main") -> dict:
        if getattr(self, "pipeline_raises", None):
            raise self.pipeline_raises
        return {"id": 99, "status": "pending", "ref": ref}

    def get_pipeline(self, project_id, pipeline_id) -> dict:
        return {"id": pipeline_id, "status": self.pipeline_status}

    def get_pipeline_jobs(self, project_id, pipeline_id) -> list:
        return [{"id": 1, "name": "test", "status": self.pipeline_status}]

    def create_mr(self, project_id, source_branch, target_branch, title, description) -> dict:
        item = {
            "project_id": project_id, "source_branch": source_branch, "target_branch": target_branch,
            "title": title, "description": description, "iid": len(self.created_mrs) + 1,
            "sha": f"sha-{len(self.created_mrs) + 1}",
            "web_url": f"http://gitlab.mock/-/merge_requests/{len(self.created_mrs) + 1}",
        }
        self.created_mrs.append(item)
        return item

    def get_mr(self, project_id, mr_iid) -> dict:
        return {"project_id": project_id, "iid": mr_iid, "state": "opened", "sha": f"sha-{mr_iid}"}

    def merge_mr(self, project_id, mr_iid, sha=None) -> dict:
        item = {"project_id": project_id, "iid": mr_iid, "state": "merged", "sha": sha}
        self.merged.append(item)
        return item
