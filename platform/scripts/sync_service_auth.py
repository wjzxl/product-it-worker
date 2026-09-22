#!/usr/bin/env python3
"""把统一的服务间鉴权模块同步到各服务（`platform/services/_shared/service_auth.py` → `<svc>/app/`）。

为什么是"复制 + 漂移检查"而不是共享包：本仓库每个服务是独立可部署单元（各自 venv 与
pyproject），跨服务 import 会把"能不能独立跑起来"这件事搞复杂。复制一份 60 行的纯标准库
模块、再用本脚本保证 6 份字节一致，是这个小团队规模下更划算的取舍。

用法：
    python3 platform/scripts/sync_service_auth.py            # 同步（写入各服务）
    python3 platform/scripts/sync_service_auth.py --check    # 只检查漂移（CI 用，漂移退出码 1）
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
CANONICAL = PLATFORM / "services" / "_shared" / "service_auth.py"

# 需要服务间鉴权的服务（approval-gateway 另有 AUTH_MODE/OIDC 人审鉴权，同样统一接入）
SERVICES = ["agent-runtime", "approval-gateway", "gitlab-bridge", "knowledge-base",
            "metrics-dashboard", "process-mining", "task-center"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写入")
    args = ap.parse_args()

    if not CANONICAL.exists():
        print(f"FAIL: 找不到真源 {CANONICAL}")
        return 1
    canonical_digest = _digest(CANONICAL)

    drifted, missing, synced = [], [], []
    for svc in SERVICES:
        target = PLATFORM / "services" / svc / "app" / "service_auth.py"
        if not target.parent.is_dir():
            missing.append(f"{svc}（服务目录不存在）")
            continue
        if not target.exists():
            missing.append(svc)
            if not args.check:
                target.write_bytes(CANONICAL.read_bytes())
                synced.append(svc)
            continue
        if _digest(target) != canonical_digest:
            drifted.append(svc)
            if not args.check:
                target.write_bytes(CANONICAL.read_bytes())
                synced.append(svc)

    if args.check:
        if drifted or missing:
            print(f"FAIL: 服务间鉴权模块与真源不一致 —— 漂移 {drifted or '无'}；缺失 {missing or '无'}")
            print("修复：python3 platform/scripts/sync_service_auth.py")
            return 1
        print(f"OK: {len(SERVICES)} 个服务的 service_auth.py 与真源一致（{canonical_digest[:12]}）")
        return 0

    print(f"已同步 {len(synced)} 个服务：{synced or '无变化'}")
    print(f"真源指纹 {canonical_digest[:12]}；服务清单 {SERVICES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
