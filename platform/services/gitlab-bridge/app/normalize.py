"""GitLab webhook payload → 平台事件归一化（契约见 contracts/events.md）。

幂等约定：事件 trace_id 取 GitLab 的 X-Gitlab-Event-UUID 头，
消费方按 (source, trace_id) 去重，天然抵抗 GitLab 的重试投递。
"""
from __future__ import annotations

from .events import new_event

_SOURCE = "gitlab-webhook"


def _issue_payload(payload: dict) -> dict:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    return {
        "project_id": project.get("id"),
        "path_with_namespace": project.get("path_with_namespace", ""),
        "iid": attrs.get("iid"),
        "title": attrs.get("title"),
        "description": attrs.get("description", ""),
        "labels": [label.get("title") for label in (attrs.get("labels") or [])],
    }


def _mr_payload(payload: dict) -> dict:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    return {
        "project_id": project.get("id"),
        "path_with_namespace": project.get("path_with_namespace", ""),
        "iid": attrs.get("iid"),
        "title": attrs.get("title"),
        "source_branch": attrs.get("source_branch"),
        "target_branch": attrs.get("target_branch"),
    }


def normalize(payload: dict, gitlab_event_uuid: str | None = None) -> list[dict]:
    """把一条 GitLab webhook 载荷归一化为 0..n 条平台事件。

    不认识的事件类型（如 note）返回空列表——桥只负责转发契约内的事件。
    """
    kind = payload.get("object_kind")
    trace_id = f"gl-{gitlab_event_uuid}" if gitlab_event_uuid else None
    attrs = payload.get("object_attributes") or {}
    action = attrs.get("action")

    if kind == "issue":
        if action == "open":
            return [new_event("gitlab.issue.opened", _issue_payload(payload), _SOURCE, trace_id=trace_id)]
        if action in ("update", "reopen", "close"):
            data = _issue_payload(payload)
            data["state"] = attrs.get("state")
            data["changes"] = payload.get("changes") or {}
            return [new_event("gitlab.issue.updated", data, _SOURCE, trace_id=trace_id)]

    if kind == "merge_request":
        if action == "open":
            return [new_event("gitlab.mr.opened", _mr_payload(payload), _SOURCE, trace_id=trace_id)]
        if action == "merge":
            data = _mr_payload(payload)
            data["merge_commit_sha"] = attrs.get("merge_commit_sha")
            return [new_event("gitlab.mr.merged", data, _SOURCE, trace_id=trace_id)]

    if kind == "pipeline":
        status = attrs.get("status")
        if status in ("success", "failed", "canceled", "skipped"):  # 终态
            project = payload.get("project") or {}
            return [
                new_event(
                    "gitlab.pipeline.finished",
                    {
                        "project_id": project.get("id"),
                        "path_with_namespace": project.get("path_with_namespace", ""),
                        "pipeline_id": attrs.get("id"),
                        "status": status,
                        "ref": attrs.get("ref"),
                        "sha": attrs.get("sha"),
                    },
                    _SOURCE,
                    trace_id=trace_id,
                )
            ]

    return []
