"""GitLab 双向集成桥（T0.2）。

方向一（入）：GitLab webhook → 平台事件，POST /webhooks/gitlab
方向二（出）：平台 → GitLab API，/gitlab/* 辅助端点（演示流与 Agent 调用）
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .client import GitLabAPIError, GitLabClient
from .events import new_event, publisher_from_env
from .normalize import normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class Settings:
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    webhook_secret: str = ""
    publish_targets: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        raw_targets = os.getenv("PUBLISH_TARGETS", "")
        return cls(
            gitlab_url=os.getenv("GITLAB_URL", "https://gitlab.com"),
            gitlab_token=os.getenv("GITLAB_TOKEN", ""),
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            publish_targets=[t.strip() for t in raw_targets.split(",") if t.strip()],
        )


class IssueIn(BaseModel):
    project_id: Union[str, int]
    title: str = Field(min_length=1)
    description: str = ""
    labels: list[str] = []


class NoteIn(BaseModel):
    project_id: Union[str, int]
    mr_iid: int
    body: str = Field(min_length=1)


class CreateMrIn(BaseModel):
    project_id: Union[str, int]
    source_branch: str = Field(min_length=1)
    target_branch: str = "main"
    title: str = Field(min_length=1)
    description: str = ""


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="gitlab-bridge",
        description="GitLab 双向集成桥：webhook→平台事件；平台→GitLab API",
        version="0.1.0",
    )
    app.state.settings = settings
    app.state.gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    app.state.publisher = publisher_from_env(settings.publish_targets)

    # ---------- 方向一：GitLab → 平台 ----------
    @app.post("/webhooks/gitlab")
    async def gitlab_webhook(request: Request) -> dict:
        token = request.headers.get("x-gitlab-token", "")
        if not settings.webhook_secret or not secrets.compare_digest(token, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook token")

        payload = await request.json()
        event_uuid = request.headers.get("x-gitlab-event-uuid")
        events = normalize(payload, event_uuid)
        for ev in events:
            # 发布失败只记录（FanoutPublisher 内部吞错），webhook 始终 200，
            # GitLab 重试由消费方幂等去重兜底。
            app.state.publisher.publish(ev)
        logger.info("webhook accepted: %s events=%s", event_uuid, [e["event_type"] for e in events])
        return {"status": "accepted", "events": [e["event_type"] for e in events]}

    # ---------- 方向二：平台 → GitLab ----------
    @app.post("/gitlab/issues")
    def create_issue(body: IssueIn) -> dict:
        try:
            return app.state.gitlab.create_issue(body.project_id, body.title, body.description, body.labels)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/gitlab/issues/{project_id}/{iid}")
    def get_issue(project_id: str, iid: int) -> dict:
        try:
            return app.state.gitlab.get_issue(project_id, iid)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/gitlab/mrs/notes")
    def comment_mr(body: NoteIn) -> dict:
        try:
            return app.state.gitlab.comment_mr(body.project_id, body.mr_iid, body.body)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/gitlab/mrs")
    def create_mr(body: CreateMrIn) -> dict:
        try:
            return app.state.gitlab.create_mr(
                body.project_id, body.source_branch, body.target_branch, body.title, body.description
            )
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/gitlab/mrs/{project_id}/{mr_iid}")
    def get_mr(project_id: str, mr_iid: int) -> dict:
        try:
            return app.state.gitlab.get_mr(project_id, mr_iid)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/gitlab/mrs/{project_id}/{mr_iid}/merge")
    def merge_mr(project_id: str, mr_iid: int, payload: Optional[dict] = None) -> dict:
        """合入 MR。GitLab 新版要求携带 sha（源分支头提交），由调用方先查 MR 获取。"""
        try:
            sha = (payload or {}).get("sha") or None
            return app.state.gitlab.merge_mr(project_id, mr_iid, sha=sha)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/gitlab/pipelines/{project_id}/{pipeline_id}")
    def get_pipeline(project_id: str, pipeline_id: int) -> dict:
        try:
            return app.state.gitlab.get_pipeline(project_id, pipeline_id)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "gitlab-bridge", "version": "0.1.0"}

    return app


app = create_app(Settings.from_env())
