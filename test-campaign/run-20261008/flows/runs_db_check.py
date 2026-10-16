#!/usr/bin/env python3
"""运行归档是否落在平台库（度量读得到）？

背景：`agent-runtime` 的归档库由 `RUNS_DB_URL` 优先决定，其次 `DATABASE_URL`。
两者不一致时，运行记录会静默落到另一个库——而驾驶舱/度量从平台库读，会显示"0 次运行"，
运维据此判断"系统没在跑"，是典型的静默错误。

用法：python3 flows/runs_db_check.py
判定：平台库应包含 agent_runs 且行数 > 0（本战役已经跑过运行）。
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
import json

PLATFORM_DB = "pst_sut"


def psql(sql: str) -> str:
    proc = subprocess.run(["docker", "exec", "platform-pg", "psql", "-U", "platform",
                           "-d", PLATFORM_DB, "-tAc", sql], capture_output=True, text=True)
    return proc.stdout.strip()


def main() -> int:
    tables = psql("select count(*) from pg_tables where schemaname='public' and tablename='agent_runs'")
    if tables != "1":
        print(f"RESULT: FAIL 平台库没有 agent_runs 表——运行归档不在度量读的库里")
        return 1
    rows = int(psql("select count(*) from agent_runs") or 0)
    req = urllib.request.Request("http://127.0.0.1:18002/runs?limit=100",
                                 headers={"X-Service-Token": "pst-sut-token"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        api_runs = len(json.load(resp).get("runs") or [])
    print(f"  平台库 agent_runs 行数 = {rows}；agent-runtime API 可见运行数 = {api_runs}")
    if rows == 0 and api_runs > 0:
        print("RESULT: FAIL 运行存在但不在平台库：度量会显示 0 次运行（RUNS_DB_URL 与 DATABASE_URL 不一致）")
        return 1
    print(f"RESULT: PASS 运行归档与平台库一致（{rows} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
