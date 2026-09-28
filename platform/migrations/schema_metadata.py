"""平台库 metadata 收集（`env.py` 与 `scripts/check_migrations.py` 共用这一份口径）。

平台库是**三个服务共享**的库：approval-gateway（审批/闸门/证据/信任）、
task-center（任务/时间线/事件收件箱）、agent-runtime（运行归档）。
三个服务都用 `app.models` 这个模块名（各自是独立可部署单元），同一进程里直接
`import app.models` 三次只会拿到第一份。这里的做法：**逐个服务**把它放进 sys.path、
清掉 `app*` 缓存再导入，取出 metadata 后立刻清掉。只取 metadata（不共享类对象），
因此互不干扰。

注意：`checkpoints*` 四张表由 LangGraph 的 PostgresSaver 自己 `setup()` 管理，
`litellm` 库由 LiteLLM 自己管理——**都不属于本迁移的管辖范围**，比对时不要把它们的缺失当成漂移。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parent.parent / "services"
SERVICE_PACKAGES = ("approval-gateway", "task-center", "agent-runtime")
EXTERNAL_TABLES = {"alembic_version", "checkpoints", "checkpoint_blobs",
                   "checkpoint_writes", "checkpoint_migrations"}


def merge_metadata(metadatas: list):
    """把多份 metadata 合成一份，供 `compare_metadata` 使用。

    为什么必须合并：`compare_metadata(ctx, md)` 只认一份 metadata，库里存在但该份
    metadata 里没有的表会被报成 `remove_table`。三份分开比对 ⇒ 每份都会把另两个服务的
    表当成"被删掉的表"，得到 60 处假差异。合并后才是真实漂移。
    """
    from sqlalchemy import MetaData

    combined = MetaData()
    for md in metadatas:
        for table in md.tables.values():
            if table.name not in combined.tables:
                table.to_metadata(combined)
    return combined


def collect_metadata(default_dsn: str = "sqlite:///./migrations_dummy.db") -> list:
    dsn = os.getenv("ALEMBIC_DATABASE_URL") or os.getenv("DATABASE_URL") or default_dsn
    os.environ.setdefault("DATABASE_URL", dsn)      # 服务的 db.py 在导入期就会读它
    os.environ.setdefault("RUNS_DB_URL", dsn)       # agent-runtime 的 runs.py 读这个

    metadatas = []
    for service in SERVICE_PACKAGES:
        service_dir = str(SERVICES_DIR / service)
        if not (SERVICES_DIR / service / "app").is_dir():
            continue
        sys.path.insert(0, service_dir)
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        try:
            if service == "agent-runtime":
                from app import runs as app_runs          # noqa: PLC0415
                metadatas.append(app_runs.Base.metadata)
            else:
                from app import models as app_models      # noqa: PLC0415
                metadatas.append(app_models.Base.metadata)
        except Exception as exc:  # noqa: BLE001 —— 某个服务依赖缺失不该让迁移完全不可用
            print(f"[migrations] 跳过 {service} 的 metadata：{type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            sys.path.remove(service_dir)
            for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
                del sys.modules[name]
    return metadatas
