"""证据库（WP5）：验收项 ↔ 可复验工件。

对应课程图 33：清单写具体操作和预期结果，并保存相应截图、日志或导出文件——
**不能只写"已测"**；需求、代码产物和测试证据要互相对应，半年后接手的人要看得到证明。

设计取舍（见 17 号计划 §2 架构选型）：
- P0 不新起服务：元数据落 approval-gateway 的 PG（复用租户/权限/审计），工件落本地卷；
- 接口保持"可搬家"：上传/下载/校验都走 HTTP，将来换对象存储（S3/MinIO）只改存储实现；
- 工件按 `<tenant>/<task_ref>/` 分目录，与多产品线隔离一致；
- **证据必须可复验**：记录 sha256 与大小，下载时校验完整性；记录谁在何时上传。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import EvidenceArtifact

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "evidence"
MAX_BYTES = int(os.getenv("EVIDENCE_MAX_BYTES", str(20 * 1024 * 1024)))  # 单件 20MB
ALLOWED_KINDS = ("export", "log", "screenshot", "test_report", "doc", "other")
ALLOWED_SUFFIXES = (
    ".csv", ".xlsx", ".xls", ".json", ".txt", ".log", ".md",
    ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".html", ".xml", ".zip",
)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class EvidenceError(ValueError):
    """证据入库失败（类型/大小/路径等问题，需明确告知调用方）。"""


def root_dir() -> Path:
    env = os.getenv("EVIDENCE_DIR", "")
    return Path(env) if env else DEFAULT_ROOT


def _safe_name(name: str) -> str:
    base = Path(name or "evidence").name
    cleaned = _SAFE.sub("_", base).strip("._") or "evidence"
    return cleaned[:120]


def store(
    db: Session,
    *,
    tenant: str,
    task_ref: str,
    acceptance_item: str,
    kind: str,
    filename: str,
    content: bytes,
    created_by: str = "",
    note: str = "",
) -> dict:
    """保存一件证据并登记元数据。

    - `acceptance_item`：对应的验收项 id（E3 据此判断"每个验收项是否都有证据"）；
    - 同一条验收项可有多件证据（如导出文件 + 日志）；
    - 返回元数据（含 sha256，供复验）。
    """
    if kind not in ALLOWED_KINDS:
        raise EvidenceError(f"证据类型必须是 {ALLOWED_KINDS} 之一，收到 {kind!r}")
    if not acceptance_item.strip():
        raise EvidenceError("必须指明该证据对应的验收项（acceptance_item）")
    if not content:
        raise EvidenceError("证据内容为空")
    if len(content) > MAX_BYTES:
        raise EvidenceError(f"证据超过大小上限 {MAX_BYTES // (1024 * 1024)}MB（实际 {len(content)} 字节）")
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise EvidenceError(f"不支持的文件类型 {suffix}（允许：{', '.join(ALLOWED_SUFFIXES)}）")

    digest = hashlib.sha256(content).hexdigest()
    artifact_id = f"evd_{uuid.uuid4().hex[:12]}"
    safe = _safe_name(filename)
    target_dir = root_dir() / tenant / _SAFE.sub("_", task_ref or "unassigned")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{artifact_id}-{safe}"
    path.write_bytes(content)

    row = EvidenceArtifact(
        id=artifact_id,
        tenant=tenant,
        task_ref=task_ref,
        acceptance_item=acceptance_item,
        kind=kind,
        filename=safe,
        path=str(path),
        sha256=digest,
        size=len(content),
        created_by=created_by,
        note=note[:500],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("证据入库 %s task=%s item=%s kind=%s size=%d", artifact_id, task_ref, acceptance_item, kind, len(content))
    return to_dict(row)


def to_dict(row: EvidenceArtifact) -> dict:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "task_ref": row.task_ref,
        "acceptance_item": row.acceptance_item,
        "kind": row.kind,
        "filename": row.filename,
        "sha256": row.sha256,
        "size": row.size,
        "created_by": row.created_by,
        "note": row.note,
        "created_at": row.created_at.isoformat(),
        "download_url": f"/evidence/{row.id}/content",
    }


def list_for_task(db: Session, tenant: str, task_ref: str) -> list[dict]:
    rows = db.scalars(
        select(EvidenceArtifact)
        .where(EvidenceArtifact.tenant == tenant, EvidenceArtifact.task_ref == task_ref)
        .order_by(EvidenceArtifact.created_at.asc())
    )
    return [to_dict(r) for r in rows]


def get(db: Session, tenant: str, artifact_id: str) -> Optional[EvidenceArtifact]:
    row = db.get(EvidenceArtifact, artifact_id)
    if row is None or row.tenant != tenant:
        return None
    return row


def read_content(row: EvidenceArtifact) -> bytes:
    path = Path(row.path)
    if not path.exists():
        raise EvidenceError(f"证据文件丢失：{row.path}（元数据存在但工件不在，需重新上传）")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise EvidenceError(f"证据完整性校验失败：{row.id}（文件被改动或损坏）")
    return data


def verify(db: Session, tenant: str, task_ref: str) -> dict:
    """复验报告：逐件校验 hash 是否仍然一致（课程图 33：证据要能复验）。"""
    rows = db.scalars(
        select(EvidenceArtifact).where(EvidenceArtifact.tenant == tenant, EvidenceArtifact.task_ref == task_ref)
    )
    ok, broken = [], []
    for row in rows:
        try:
            read_content(row)
            ok.append(row.id)
        except EvidenceError as exc:
            broken.append({"id": row.id, "error": str(exc)})
    return {"task_ref": task_ref, "total": len(ok) + len(broken), "verified": len(ok), "broken": broken}


def evidence_map(db: Session, tenant: str, task_ref: str) -> dict[str, list[str]]:
    """验收项 → 证据 id 列表（E3 闸门据此判定）。"""
    mapping: dict[str, list[str]] = {}
    for item in list_for_task(db, tenant, task_ref):
        mapping.setdefault(item["acceptance_item"], []).append(item["id"])
    return mapping
