"""GitLab 双向集成桥（T0.2）。

方向一（入）：GitLab webhook → 平台事件，POST /webhooks/gitlab
方向二（出）：平台 → GitLab API，/gitlab/* 辅助端点（演示流与 Agent 调用）
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import json
from dataclasses import dataclass, field
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .client import GitLabAPIError, GitLabClient
from .events import new_event, publisher_from_env
from .normalize import normalize
from .service_auth import install_service_auth, security_posture

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


class HookIn(BaseModel):
    project_id: Union[str, int]
    url: str = Field(min_length=1, description="平台公网 webhook 地址")
    secret: str = ""
    events: list[str] = Field(default_factory=lambda: ["issues_events", "merge_requests_events", "pipeline_events"])


class TriggerPipelineIn(BaseModel):
    project_id: Union[str, int]
    ref: str = "main"


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
    # 入站 webhook 自带 Token/HMAC 校验（见 /webhooks/gitlab），故豁免服务令牌
    install_service_auth(app, "gitlab-bridge", extra_exempt=("/webhooks/gitlab",))
    app.state.settings = settings
    app.state.gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    app.state.publisher = publisher_from_env(settings.publish_targets)

    # ---------- 方向一：GitLab → 平台 ----------
    @app.post("/webhooks/gitlab")
    async def gitlab_webhook(request: Request) -> dict:
        """公网 webhook 入口：支持 Token 校验与 HMAC-SHA256 签名校验（生产推荐签名）。"""
        raw = await request.body()
        token = request.headers.get("x-gitlab-token", "")
        signature = request.headers.get("x-gitlab-signature", "")
        token_ok = bool(settings.webhook_secret) and secrets.compare_digest(token, settings.webhook_secret)
        signature_ok = False
        if settings.webhook_secret and signature:
            expected = hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
            provided = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
            signature_ok = secrets.compare_digest(provided, expected)
        if not (token_ok or signature_ok):
            raise HTTPException(status_code=401, detail="invalid webhook token/signature")

        payload = json.loads(raw or b"{}")
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

    @app.post("/gitlab/issues/{project_id}/{iid}/close")
    def close_issue(project_id: str, iid: int) -> dict:
        """关闭 Issue（缺陷闭环：修复验证通过后关闭缺陷单）。"""
        try:
            return app.state.gitlab.close_issue(project_id, iid)
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

    @app.get("/gitlab/pipelines/{project_id}/{pipeline_id}/jobs")
    def get_pipeline_jobs(project_id: str, pipeline_id: int) -> list:
        try:
            return app.state.gitlab.get_pipeline_jobs(project_id, pipeline_id)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/gitlab/pipelines/trigger")
    def trigger_pipeline(body: TriggerPipelineIn) -> dict:
        """触发真实流水线（CI/CD 发布执行）。GitLab 侧限制（如账号未实名/Runner 缺失）会以 502+原因返回。"""
        try:
            return app.state.gitlab.trigger_pipeline(body.project_id, body.ref)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/gitlab/pipelines/{project_id}")
    def list_pipelines(project_id: str, per_page: int = 5) -> list:
        try:
            return app.state.gitlab.list_pipelines(project_id, per_page)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/gitlab/hooks")
    def register_hook(body: HookIn) -> dict:
        """自动注册 webhook（部署脚本调用）：目标 URL 指向平台公网地址。"""
        try:
            return app.state.gitlab.create_hook(
                body.project_id, body.url, body.secret, body.events
            )
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/gitlab/hooks/{project_id}")
    def list_hooks(project_id: str) -> list:
        try:
            return app.state.gitlab.list_hooks(project_id)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.delete("/gitlab/hooks/{project_id}/{hook_id}")
    def delete_hook(project_id: str, hook_id: int) -> dict:
        try:
            return app.state.gitlab.delete_hook(project_id, hook_id)
        except GitLabAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "gitlab-bridge", "version": "0.1.0",
                "security": security_posture("gitlab-bridge")}

    return app


app = create_app(Settings.from_env())
