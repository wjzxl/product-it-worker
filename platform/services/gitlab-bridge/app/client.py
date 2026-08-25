"""GitLab REST API 客户端（Phase 0 最小集：Issue / MR 评论 / Pipeline 查询）。

认证：PRIVATE-TOKEN 头（个人令牌或项目令牌）；project_id 支持数字或 URL 编码的组/项目路径。
"""
from __future__ import annotations

import httpx
from urllib.parse import quote


class GitLabAPIError(RuntimeError):
    """GitLab API 返回 >=400。"""


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {"PRIVATE-TOKEN": token} if token else {}
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise GitLabAPIError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    @staticmethod
    def _encode(project_id) -> str:
        return quote(str(project_id), safe="")

    # ---------- Issue ----------
    def create_issue(
        self, project_id, title: str, description: str = "", labels: list[str] | None = None
    ) -> dict:
        return self._request(
            "POST",
            f"/api/v4/projects/{self._encode(project_id)}/issues",
            json={"title": title, "description": description, "labels": ",".join(labels or [])},
        )

    def get_issue(self, project_id, iid: int) -> dict:
        return self._request("GET", f"/api/v4/projects/{self._encode(project_id)}/issues/{iid}")

    def update_issue(self, project_id, iid: int, **fields) -> dict:
        return self._request(
            "PUT", f"/api/v4/projects/{self._encode(project_id)}/issues/{iid}", json=fields
        )

    # ---------- Merge Request ----------
    def comment_mr(self, project_id, mr_iid: int, body: str) -> dict:
        return self._request(
            "POST",
            f"/api/v4/projects/{self._encode(project_id)}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )

    def create_mr(
        self, project_id, source_branch: str, target_branch: str, title: str, description: str = ""
    ) -> dict:
        return self._request(
            "POST",
            f"/api/v4/projects/{self._encode(project_id)}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )

    def get_mr(self, project_id, mr_iid: int) -> dict:
        return self._request(
            "GET", f"/api/v4/projects/{self._encode(project_id)}/merge_requests/{mr_iid}"
        )

    def merge_mr(self, project_id, mr_iid: int, sha: str | None = None) -> dict:
        payload = {"sha": sha} if sha else {}
        return self._request(
            "PUT",
            f"/api/v4/projects/{self._encode(project_id)}/merge_requests/{mr_iid}/merge",
            json=payload,
        )

    # ---------- Pipeline ----------
    def get_pipeline(self, project_id, pipeline_id: int) -> dict:
        return self._request(
            "GET", f"/api/v4/projects/{self._encode(project_id)}/pipelines/{pipeline_id}"
        )
