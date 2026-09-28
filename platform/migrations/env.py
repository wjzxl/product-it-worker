"""Alembic 环境：平台库是**三个服务共享**的库，所以这里要一次性收集三份 metadata。

为什么需要迁移机制（而不是继续 `create_all` + 手工 `ALTER`）：
- `create_all` 只能建"缺的表"，改不了已有表的列——线上加一个字段要靠人记得手写 ALTER；
- 没有版本记录 ⇒ 无法回答"这个库现在是什么版本""升级到哪一步失败了""怎么回滚"；
- 多版本滚动升级/回滚是上线的硬前提。

用哪个 venv 跑：`services/approval-gateway/.venv`（它带 alembic）。平台库的 schema 视为
**仓库级资产**（不属于某一个服务），因此迁移目录放在 `platform/migrations/`，不放进服务内。

    cd platform
    services/approval-gateway/.venv/bin/python -m alembic upgrade head
    services/approval-gateway/.venv/bin/python -m alembic revision --autogenerate -m "说明"

连接串来源（优先级）：`ALEMBIC_DATABASE_URL` > `DATABASE_URL` > alembic.ini 的 sqlalchemy.url。
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

PLATFORM = Path(__file__).resolve().parent.parent

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_metadata import collect_metadata  # noqa: E402  （与 scripts/check_migrations.py 同一份口径）

target_metadata = collect_metadata()


def _database_url() -> str:
    return (os.getenv("ALEMBIC_DATABASE_URL") or os.getenv("DATABASE_URL")
            or config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 不支持 ALTER 大多数列：批处理模式让它用"建新表→拷数据→换名"实现
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
