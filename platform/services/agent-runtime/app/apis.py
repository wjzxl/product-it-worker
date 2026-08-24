"""外部服务 HTTP 客户端（可注入 transport，测试用 mock）。"""
from __future__ import annotations

from typing import Any

import httpx


class ApprovalAPI:
    """审批网关客户端。"""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30, transport=transport)

    def create_approval(self, payload: dict) -> dict:
        resp = self._client.post(f"{self.base_url}/approvals", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_approval(self, approval_id: str) -> dict:
        resp = self._client.get(f"{self.base_url}/approvals/{approval_id}")
        resp.raise_for_status()
        return resp.json()


class GitLabAPI:
    """GitLab 集成桥客户端（桥接 /gitlab/* 辅助端点）。"""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30, transport=transport)

    def create_issue(self, project_id: Any, title: str, description: str, labels: list[str]) -> dict:
        resp = self._client.post(
            f"{self.base_url}/gitlab/issues",
            json={
                "project_id": project_id,
                "title": title,
                "description": description,
                "labels": labels,
            },
        )
        resp.raise_for_status()
        return resp.json()


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

    def create_issue(self, project_id, title, description, labels) -> dict:
        item = {"project_id": project_id, "title": title, "description": description, "labels": labels, "iid": len(self.created) + 12}
        self.created.append(item)
        return item
