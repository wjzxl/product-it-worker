#!/usr/bin/env python3
"""运行时建表（create_all）与迁移建表（alembic）必须一致。

为什么值得单独一张卡：两条路径分别由「服务启动」和「部署流程」使用。
它们一旦漂移，新环境（走迁移）与老环境（走 create_all）就会得到不同的 schema，
而症状会延迟到某个字段报错时才出现。

用法：python3 flows/schema_compare.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLATFORM = ROOT / "platform"
SCRATCH = "mig_cmp_camp1008"
RUNTIME_DB = "pst_sut"


def docker_psql(db: str, sql: str) -> str:
    proc = subprocess.run(["docker", "exec", "platform-pg", "psql", "-U", "platform",
                           "-d", db, "-tAc", sql], capture_output=True, text=True)
    return proc.stdout.strip()


def tables(db: str, exclude_alembic: bool = False) -> list[str]:
    where = "schemaname='public'"
    if exclude_alembic:
        where += " and tablename<>'alembic_version'"
    out = docker_psql(db, f"select tablename from pg_tables where {where} order by 1")
    return [t for t in out.splitlines() if t]


def main() -> int:
    docker_psql("postgres", f'DROP DATABASE IF EXISTS "{SCRATCH}"')
    docker_psql("postgres", f'CREATE DATABASE "{SCRATCH}"')
    dsn = f"postgresql+psycopg://platform:platform@127.0.0.1:5432/{SCRATCH}"
    proc = subprocess.run(
        [str(PLATFORM / "services/approval-gateway/.venv/bin/python"), "-m", "alembic",
         "upgrade", "head"],
        cwd=str(PLATFORM), capture_output=True, text=True,
        env={**__import__("os").environ, "ALEMBIC_DATABASE_URL": dsn})
    if proc.returncode != 0:
        print(f"RESULT: FAIL alembic upgrade head 失败：{proc.stderr.strip()[-300:]}")
        return 1
    migrated = tables(SCRATCH, exclude_alembic=True)

    # 对照物：**模型元数据**在另一个空库里 create_all 出来的表
    # （比"服务恰好写到了哪个库"更纯粹：这里问的是"迁移与模型是否一致"）
    model_dsn = f"postgresql+psycopg://platform:platform@127.0.0.1:5432/{SCRATCH}_models"
    docker_psql("postgres", f'DROP DATABASE IF EXISTS "{SCRATCH}_models"')
    docker_psql("postgres", f'CREATE DATABASE "{SCRATCH}_models"')
    # 系统 python3 没有 sqlalchemy：借 approval-gateway 的 venv 跑这段
    helper = (
        "import sys; sys.path.insert(0, %r)\n"
        "from schema_metadata import collect_metadata\n"
        "from sqlalchemy import create_engine\n"
        "e = create_engine(%r)\n"
        "[md.create_all(bind=e) for md in collect_metadata(%r)]\n"
        % (str(PLATFORM / "migrations"), model_dsn, model_dsn))
    proc2 = subprocess.run([str(PLATFORM / "services/approval-gateway/.venv/bin/python"),
                            "-c", helper], capture_output=True, text=True, cwd=str(PLATFORM))
    if proc2.returncode != 0:
        print(f"RESULT: FAIL 模型建表失败：{proc2.stderr.strip()[-300:]}")
        return 1
    modelled = tables(f"{SCRATCH}_models", exclude_alembic=True)
    docker_psql("postgres", f'DROP DATABASE IF EXISTS "{SCRATCH}"')
    docker_psql("postgres", f'DROP DATABASE IF EXISTS "{SCRATCH}_models"')

    only_migrated = sorted(set(migrated) - set(modelled))
    only_modelled = sorted(set(modelled) - set(migrated))
    print(f"  迁移建表 {len(migrated)} 张：{migrated}")
    print(f"  模型建表 {len(modelled)} 张：{modelled}")
    if only_migrated or only_modelled:
        print(f"RESULT: FAIL schema 漂移：仅迁移有={only_migrated} 仅模型有={only_modelled}")
        return 1
    print(f"RESULT: PASS 迁移与模型一致（{len(migrated)} 张表）")

    # 顺带如实报告：运行时**实际**把运行归档写到哪个库（可能不是被迁移的那个）
    runtime = tables(RUNTIME_DB)
    missing = sorted(set(migrated) - set(runtime))
    if missing:
        print(f"  注意：运行时库 {RUNTIME_DB} 缺少 {missing}"
              f"（agent-runtime 的 RUNS_DB_URL 指向别处时会出现；度量从平台库读会看到 0 次运行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
