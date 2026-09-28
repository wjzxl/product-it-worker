#!/usr/bin/env python3
"""迁移体检：**模型与迁移不许漂移**，空库能升到当前版本，升级可回滚。

为什么需要它：迁移机制最大的失败模式不是"写不出迁移"，而是**模型改了、迁移忘了写**——
于是新环境建出来的库和代码期望的库不一致，直到某个字段报错才被发现。
这里用 Alembic 自己的 autogenerate 比对器把这件事变成 CI 里的一条断言：

    upgrade head  →  compare_metadata 必须没有差异  →  downgrade base 干净回滚

用法（需要一个带 alembic 的 venv，见 migrations/README.md）：
    services/approval-gateway/.venv/bin/python scripts/check_migrations.py
    services/approval-gateway/.venv/bin/python scripts/check_migrations.py --selftest
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
MIGRATIONS = PLATFORM / "migrations"
ALEMBIC_INI = PLATFORM / "alembic.ini"
SERVICES = ("approval-gateway", "task-center", "agent-runtime")


def _alembic(args: list[str], dsn: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": dsn}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
                          cwd=PLATFORM, env=env, capture_output=True, text=True)


def _load_metadata(dsn: str):
    """复用 migrations/schema_metadata.py 的收集逻辑（与 env.py 同一份口径）。"""
    os.environ.setdefault("DATABASE_URL", dsn)
    os.environ.setdefault("RUNS_DB_URL", dsn)
    sys.path.insert(0, str(MIGRATIONS))
    from schema_metadata import EXTERNAL_TABLES, collect_metadata, merge_metadata  # noqa: PLC0415

    metadatas = collect_metadata(dsn)
    return merge_metadata(metadatas), metadatas, EXTERNAL_TABLES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="自检：故意制造差异，检查必须能发现")
    args = ap.parse_args()

    from sqlalchemy import create_engine, inspect
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    tmp = Path(tempfile.mkdtemp(prefix="mig-check-"))
    dsn = f"sqlite:///{tmp}/mig.db"
    checks: list[tuple[str, bool, str]] = []

    up = _alembic(["upgrade", "head"], dsn)
    ok_up = up.returncode == 0
    checks.append(("空库 upgrade head 成功", ok_up, (up.stderr or "").strip().splitlines()[-1] if up.stderr else ""))
    if not ok_up:
        for name, passed, detail in checks:
            print(f"{'✅' if passed else '❌'} {name} {detail}")
        return 1

    engine = create_engine(dsn)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}   # checkpoints* 由 LangGraph 自管
    checks.append(("建出的表非空", len(tables) >= 9, f"{len(tables)} 张：{sorted(tables)[:4]}…"))

    combined, metadatas, external = _load_metadata(dsn)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, combined)
    checks.append(("模型与迁移无漂移（改了模型必须补迁移）", not diffs,
                   f"{len(diffs)} 处差异：{str(diffs[:3])[:200]}"))

    if args.selftest:
        # 在**合并后的** metadata 上加一张迁移里没有的表：比对器必须**恰好**报出这一处
        # （报 0 处说明体检是假绿的；报一堆说明比对口径又错了）
        from sqlalchemy import Column, Integer, Table
        Table("drift_probe", combined, Column("id", Integer, primary_key=True))
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            probe_diffs = compare_metadata(ctx, combined)
        kinds = [d[0] if isinstance(d, tuple) else type(d).__name__ for d in probe_diffs]
        checks.append(("自检：故意制造的差异被精确发现（且只有这一处）",
                       kinds == ["add_table"], f"{probe_diffs}"))

    down = _alembic(["downgrade", "base"], dsn)
    left = set(inspect(engine).get_table_names()) - {"alembic_version"}
    checks.append(("downgrade base 可干净回滚", down.returncode == 0 and not left,
                   f"剩余表 {sorted(left)}" if left else ""))
    engine.dispose()

    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        print(f"{'✅' if passed else '❌'} {name}" + (f" — {detail}" if detail else ""))
    print("迁移体检通过 ✅" if ok else "迁移体检失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
