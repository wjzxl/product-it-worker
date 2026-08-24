#!/usr/bin/env python3
"""Langfuse v2 自托管初始化（Phase 0 本地验收用）。

步骤：
  1. docker compose -f docker-compose.observability.yml up -d   # 等 http://localhost:3000/api/public/health = 200
  2. 本脚本（需要 psycopg，用 approval-gateway 的 venv 运行）：
     services/approval-gateway/.venv/bin/python scripts/setup_langfuse_demo.py
  3. 脚本输出 LiteLLM 接入环境变量，export 后 docker compose up -d litellm

说明：Langfuse v2 首次注册后组织/项目需在 UI 引导中创建（交互式）；
本脚本直接按官方 schema 在本地 DB 注入演示组织/项目/API Key（仅本地验收用，
生产请使用 Langfuse Cloud 或 UI 创建）。
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid

import httpx
import psycopg

BASE = "http://localhost:3000"
DSN = "host=127.0.0.1 port=5433 dbname=langfuse user=langfuse password=langfuse"
EMAIL = "demo@local.dev"
PASSWORD = "phase0-demo-pass"
SALT = os.getenv("LANGFUSE_SALT", "phase0-dev-salt-change-me")  # 与 docker-compose.observability.yml 保持一致
PUBLIC_KEY = f"pk-lf-{uuid.uuid4().hex[:16]}"
SECRET_KEY = f"sk-lf-{uuid.uuid4().hex[:16]}"


def hash_secret(secret: str) -> str:
    """Langfuse v2 的密钥快速哈希：sha256(secret + sha256_hex(SALT))（源码 chunk 实测）。"""
    inner = hashlib.sha256(SALT.encode("utf-8")).hexdigest()
    return hashlib.sha256((secret + inner).encode("utf-8")).hexdigest()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)

    # 1) 等待就绪
    try:
        client.get("/api/public/health")
    except httpx.HTTPError:
        print("Langfuse 未就绪，请先：docker compose -f docker-compose.observability.yml up -d 并等待")
        return 1

    # 2) 注册用户（已存在则跳过）
    r = client.post("/api/auth/signup", json={"name": "Demo", "email": EMAIL, "password": PASSWORD})
    if r.status_code in (200, 201):
        print("用户已创建:", EMAIL)
    elif r.status_code == 422 and "already exists" in r.text:
        print("用户已存在:", EMAIL)
    else:
        print(f"注册失败: HTTP {r.status_code} {r.text[:200]}")
        return 1

    # 3) DB 注入：组织 / 项目 / API Key / 成员关系
    digest = hash_secret(SECRET_KEY)
    with psycopg.connect(DSN) as conn:
        user = conn.execute("select id from users where email=%s", (EMAIL,)).fetchone()
        if not user:
            print("DB 中未找到用户，注册可能未落库")
            return 1
        user_id = user[0]

        # 幂等：复用既有演示组织/项目，重建演示密钥
        org = conn.execute("select id from organizations where name='DemoOrg'").fetchone()
        if org:
            org_id = org[0]
        else:
            org_id = f"org-{uuid.uuid4().hex[:12]}"
            conn.execute("insert into organizations (id, name) values (%s, %s)", (org_id, "DemoOrg"))
        proj = conn.execute("select id from projects where name='DemoProject'").fetchone()
        if proj:
            project_id = proj[0]
        else:
            project_id = f"proj-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "insert into projects (id, name, org_id) values (%s, %s, %s)",
                (project_id, "DemoProject", org_id),
            )
        conn.execute("delete from api_keys where note='litellm-demo'")
        conn.execute(
            """insert into api_keys (id, note, public_key, hashed_secret_key, display_secret_key, project_id, fast_hashed_secret_key)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            (f"key-{uuid.uuid4().hex[:12]}", "litellm-demo", PUBLIC_KEY, digest, SECRET_KEY[:11] + "..." + SECRET_KEY[-4:], project_id, digest),
        )
        # 成员关系（让 UI 登录后可见；角色枚举为 "Role"，值 NONE/VIEWER/MEMBER/ADMIN/OWNER）
        role = "ADMIN"
        enum_ok = conn.execute(
            "select exists(select 1 from pg_enum e join pg_type t on e.enumtypid=t.oid "
            "where t.typname='Role' and e.enumlabel='ADMIN')"
        ).fetchone()[0]
        if not enum_ok:
            row = conn.execute(
                "select enumlabel from pg_enum e join pg_type t on e.enumtypid=t.oid "
                "where t.typname='Role' limit 1"
            ).fetchone()
            role = row[0] if row else "MEMBER"
        membership_id = f"mem-{uuid.uuid4().hex[:12]}"
        exists = conn.execute(
            "select id from organization_memberships where org_id=%s and user_id=%s",
            (org_id, user_id),
        ).fetchone()
        if not exists:
            conn.execute(
                "insert into organization_memberships (id, org_id, user_id, role) values (%s, %s, %s, %s)",
                (membership_id, org_id, user_id, role),
            )
        else:
            membership_id = exists[0]
        proj_mem = conn.execute(
            "select 1 from project_memberships where project_id=%s and user_id=%s",
            (project_id, user_id),
        ).fetchone()
        if not proj_mem:
            conn.execute(
                "insert into project_memberships (project_id, user_id, org_membership_id, role) values (%s, %s, %s, %s)",
                (project_id, user_id, membership_id, role),
            )
        conn.commit()
        print(f"已注入 组织={org_id} 项目={project_id} 成员={role}")

    print("\n===== 复制到启动 litellm 的 shell =====")
    print(f'export LANGFUSE_PUBLIC_KEY="{PUBLIC_KEY}"')
    print(f'export LANGFUSE_SECRET_KEY="{SECRET_KEY}"')
    print('export LANGFUSE_HOST="http://host.docker.internal:3000"')
    print('export LITELLM_SUCCESS_CALLBACK="langfuse"')
    print('export LITELLM_FAILURE_CALLBACK="langfuse"')
    print("=======================================")
    print("\n然后：cd platform && docker compose up -d litellm")
    print(f"UI: {BASE}（账号 {EMAIL} / 密码 {PASSWORD}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
