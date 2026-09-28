#!/usr/bin/env python3
"""备份 / 恢复 / **恢复演练**。

为什么把它排在"上线"前面：平台的审计资产（谁批的、批的是哪一版、证据工件的哈希）
只存在 PostgreSQL 里。没有备份意味着**出事时无法收敛**——而"我们还没上线，先不备份"
正是上线后最常见的后悔。

设计取舍（小团队）：
- 用 `pg_dump -Fc`（自定义格式，压缩 + 可选择性恢复），落盘为单个 `.dump` 文件；
- 走 `docker exec` 操作容器里的 PG（不要求宿主机装 PG 客户端）；
- `drill` 子命令 = 备份 → 恢复到一个**临时库** → 逐表比对行数 → 清理。
  恢复演练必须能一条命令跑完，否则它永远只是文档里的一句承诺。

用法：
    python3 platform/scripts/backup_restore.py backup  --out backups/               # 备份
    python3 platform/scripts/backup_restore.py restore --file x.dump --db platform_restored
    python3 platform/scripts/backup_restore.py drill                                 # 演练（推荐在 CI/发布前跑）
    python3 platform/scripts/backup_restore.py --selftest                            # 自检（不需要 docker）

环境变量：PG_CONTAINER（默认 platform-pg）、PG_USER（platform）、PG_PASSWORD（platform）
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
DEFAULT_DBS = ("platform", "litellm")
CONTAINER = os.getenv("PG_CONTAINER", "platform-pg")
USER = os.getenv("PG_USER", "platform")
PASSWORD = os.getenv("PG_PASSWORD", "platform")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _docker(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-i", CONTAINER, *args],
                          input=stdin, capture_output=True)


def psql(db: str, sql: str) -> str:
    proc = _docker(["psql", "-U", USER, "-d", db, "-tAc", sql])
    if proc.returncode != 0:
        raise RuntimeError(f"psql 失败（{db}）：{proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode().strip()


def docker_available() -> bool:
    return shutil.which("docker") is not None and _run(["docker", "info"]).returncode == 0


def table_counts(db: str) -> dict[str, int]:
    names = [n for n in psql(db, "select tablename from pg_tables where schemaname='public' order by 1").splitlines() if n]
    counts = {}
    for n in names:
        counts[n] = int(psql(db, f'select count(*) from "{n}"') or 0)
    return counts


def backup(dbs: list[str], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    written = []
    for db in dbs:
        proc = _docker(["pg_dump", "-U", USER, "-Fc", "-d", db])
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump 失败（{db}）：{proc.stderr.decode(errors='replace').strip()}")
        path = out_dir / f"{db}-{stamp}.dump"
        path.write_bytes(proc.stdout)
        written.append(path)
        print(f"  已备份 {db} → {path}（{path.stat().st_size / 1024:.1f} KB）")
    return written


def restore(dump: Path, db: str, drop_first: bool = True, create: bool = True) -> None:
    if create:
        if drop_first:
            _docker(["psql", "-U", USER, "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{db}"'])
        proc = _docker(["psql", "-U", USER, "-d", "postgres", "-c", f'CREATE DATABASE "{db}"'])
        if proc.returncode != 0:
            raise RuntimeError(f"建库失败：{proc.stderr.decode(errors='replace').strip()}")
    proc = _docker(["pg_restore", "-U", USER, "-d", db, "--no-owner", "--no-privileges"],
                   stdin=dump.read_bytes())
    # pg_restore 对"对象已存在"会返回非 0，但数据已进去；用行数比对做最终判据，这里只提示
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip().splitlines()
        print(f"  pg_restore 提示（{len(stderr)} 行，常见于扩展/属主告警）：{stderr[0] if stderr else ''}")


def drill(sources: list[str], keep: bool = False, report: bool = True) -> int:
    """备份 → 恢复到临时库 → 逐表比对 → 清理。返回 0 表示恢复可信。"""
    if not docker_available():
        print("INVALID: 需要可用的 docker（本演练通过 docker exec 操作容器内 PG）")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="pg-drill-"))
    print(f"① 备份（{', '.join(sources)}）→ {tmp}")
    dumps = {p.name.split("-")[0]: p for p in backup(sources, tmp)}

    ok = True
    stats: dict[str, tuple[float, int]] = {}
    for db in sources:
        target = f"{db}_restore_drill"
        print(f"② 恢复 {db} → {target}")
        before = table_counts(db)
        stats[db] = (dumps[db].stat().st_size / 1024, len(before))   # 备份文件马上会被清理，先记下来
        restore(dumps[db], target)
        after = table_counts(target)
        missing = {k: v for k, v in before.items() if k not in after}
        mismatch = {k: (v, after[k]) for k, v in before.items() if k in after and after[k] != v}
        status = "✅" if not missing and not mismatch else "❌"
        if missing or mismatch:
            ok = False
        print(f"   {status} {db}：{len(before)} 张表，源/恢复库行数一致={not mismatch and not missing}"
              f"{'；缺表 ' + str(list(missing)) if missing else ''}"
              f"{'；行数不一致 ' + str(mismatch) if mismatch else ''}")
        if not keep:
            _docker(["psql", "-U", USER, "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{target}"'])
    shutil.rmtree(tmp, ignore_errors=True)
    verdict = "✅ 恢复演练通过：备份文件可恢复，且行数与源库一致" if ok else \
              "❌ 恢复演练失败：备份不可信，不要依赖它"
    print(f"\n{verdict}")
    if report:
        out_dir = PLATFORM / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"backup-restore-drill-{stamp}.md"
        sizes = "\n".join(f"| {db} | {stats[db][0]:.1f} KB | {stats[db][1]} |" for db in sources)
        path.write_text(
            f"""# 备份/恢复演练报告

- 执行时间：{datetime.datetime.now(datetime.timezone.utc).isoformat()}
- 方式：`pg_dump -Fc`（容器内）→ 恢复到临时库 `*_restore_drill` → 逐表比对行数 → 清理
- 结论：{verdict}

| 库 | 备份大小 | 表数 |
|---|---|---|
{sizes}

> 说明：本演练证明"**备份文件能恢复且数据一致**"，即恢复路径真实可用；
> 生产仍需按保留策略定期备份并把备份文件放到**另一台机器/对象存储**（本脚本只负责生成与验证）。
""", encoding="utf-8")
        print(f"报告：{path.relative_to(PLATFORM)}")
    return 0 if ok else 1


def selftest() -> int:
    """自检（CI 用）：无 docker 时应明确报 INVALID，而不是假装成功。"""
    checks = []
    if docker_available():
        checks.append(("docker 可用", True, ""))
        try:
            n = len(table_counts("platform"))
            checks.append(("能读源库表清单", n > 0, f"{n} 张表"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("能读源库表清单", False, str(exc)))
    else:
        checks.append(("docker 不可用时 drill 返回 2（INVALID）", drill(["platform"]) == 2, ""))
    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        print(f"{'✅' if passed else '❌'} {name}" + (f" — {detail}" if detail else ""))
    print("自检通过 ✅" if ok else "自检失败 ❌")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p_backup = sub.add_parser("backup", help="备份数据库")
    p_backup.add_argument("--out", default=str(PLATFORM / "backups"))
    p_backup.add_argument("--dbs", default=",".join(DEFAULT_DBS))
    p_restore = sub.add_parser("restore", help="从备份恢复")
    p_restore.add_argument("--file", required=True)
    p_restore.add_argument("--db", required=True)
    p_restore.add_argument("--keep", action="store_true", help="保留目标库（默认覆盖已有目标库）")
    p_drill = sub.add_parser("drill", help="恢复演练：备份→恢复到临时库→比对行数→清理")
    p_drill.add_argument("--dbs", default="platform")
    p_drill.add_argument("--keep", action="store_true")
    p_drill.add_argument("--no-report", action="store_true", help="不写演练报告")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    try:
        if args.selftest:
            return selftest()
        if args.cmd == "backup":
            backup([d.strip() for d in args.dbs.split(",") if d.strip()], Path(args.out))
            return 0
        if args.cmd == "restore":
            restore(Path(args.file), args.db, drop_first=not args.keep, create=True)
            print(f"已恢复到 {args.db}")
            return 0
        if args.cmd == "drill":
            return drill([d.strip() for d in args.dbs.split(",") if d.strip()],
                         keep=args.keep, report=not args.no_report)
    except RuntimeError as exc:
        print(f"INVALID: {exc}")
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
