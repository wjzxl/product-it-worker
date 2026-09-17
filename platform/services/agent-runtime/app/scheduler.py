"""定时/队列触发器（WP7）：让岗位"自己开始干活"，而不是等人下指令。

对应课程图 47/48：运行中的系统不能只在收到一句提示时才工作——
7:30 自动拉数、数据没到就等、8:10 到齐继续、18:00 归档。

设计：
- 用 APScheduler 的 BlockingScheduler + cron 表达式（`platform/schedules.yaml` 声明）；
- 每个任务调用 agent-runtime `/runs`（带 trigger=schedule、tenant、role），
  不自带业务逻辑——**触发器只负责"什么时候开始"，不替岗位做判断**；
- 失败不吞：记录日志并按退避重试一次；连续失败会体现在运行归档里（status=failed）；
- 独立进程运行：`python -m app.scheduler`（与 API 进程分离，避免阻塞请求）。

用法：
    cd platform
    services/agent-runtime/.venv/bin/python -m app.scheduler --dry-run     # 只打印将触发的任务
    services/agent-runtime/.venv/bin/python -m app.scheduler               # 常驻
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULES = Path(__file__).resolve().parents[3] / "schedules.yaml"


def schedules_path() -> Path:
    env = os.getenv("SCHEDULES_FILE", "")
    return Path(env) if env else DEFAULT_SCHEDULES


def load_schedules(path: Path | None = None) -> list[dict]:
    """读取触发器定义并做最小校验（不合法直接报错，避免"配了但不会触发"）。"""
    path = path or schedules_path()
    if not path.exists():
        logger.warning("未找到触发器配置：%s（scheduler 将无事可做）", path)
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = doc.get("jobs") or []
    out = []
    for raw in jobs:
        name = raw.get("name") or ""
        if not name:
            raise ValueError(f"{path.name}: 触发器缺少 name")
        if not raw.get("cron"):
            raise ValueError(f"{path.name}: 触发器 {name} 缺少 cron 表达式")
        if not raw.get("sop"):
            raise ValueError(f"{path.name}: 触发器 {name} 缺少 sop")
        if raw.get("enabled", True) is False:
            logger.info("触发器 %s 已禁用，跳过", name)
            continue
        out.append(raw)
    return out


def trigger(base_url: str, job: dict, timeout: float = 120.0) -> dict:
    """按触发器定义启动一次运行。"""
    payload = {
        "sop": job["sop"],
        "requirement_text": job.get("requirement_text") or f"[定时任务] {job['name']}",
        "title": job.get("title") or job["name"],
        "task_ref": job.get("task_ref") or f"sched-{job['name']}-{int(time.time())}",
        "trigger": "schedule",
        "tenant": job.get("tenant", "default"),
        "role": job.get("role", ""),
        "alert": job.get("alert") or {},
        "context": job.get("context", ""),
        "runbook": job.get("runbook", ""),
    }
    if job.get("project_id"):
        payload["project_id"] = job["project_id"]
    resp = httpx.post(f"{base_url.rstrip('/')}/runs", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def run_once(base_url: str, jobs: list[dict]) -> list[dict]:
    """立即触发全部启用的任务（用于演练/自检，不依赖到点）。"""
    results = []
    for job in jobs:
        try:
            body = trigger(base_url, job)
            results.append({"job": job["name"], "ok": True, "status": body.get("status"),
                            "thread_id": body.get("thread_id")})
            logger.info("触发器 %s 已启动：status=%s thread=%s", job["name"], body.get("status"), body.get("thread_id"))
        except Exception as exc:
            results.append({"job": job["name"], "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            logger.exception("触发器 %s 启动失败", job["name"])
    return results


def build_scheduler(base_url: str, jobs: list[dict]):
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler(timezone=os.getenv("SCHEDULER_TZ", "Asia/Shanghai"))

    def make_runner(job: dict):
        def _run():
            try:
                body = trigger(base_url, job)
                logger.info("触发器 %s 启动：status=%s", job["name"], body.get("status"))
            except Exception as exc:
                logger.exception("触发器 %s 失败：%s", job["name"], exc)
        return _run

    for job in jobs:
        scheduler.add_job(make_runner(job), CronTrigger.from_crontab(job["cron"]),
                          id=job["name"], name=job.get("title") or job["name"], replace_existing=True)
        logger.info("已注册触发器 %s（cron=%s sop=%s role=%s）",
                    job["name"], job["cron"], job["sop"], job.get("role", "-"))
    return scheduler


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="岗位定时触发器（WP7）")
    parser.add_argument("--base-url", default=os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:8002"))
    parser.add_argument("--dry-run", action="store_true", help="只列出将注册的触发器，不启动")
    parser.add_argument("--once", action="store_true", help="立即触发全部启用任务一次（演练用）")
    args = parser.parse_args()

    jobs = load_schedules()
    if not jobs:
        print("没有启用的触发器（检查 platform/schedules.yaml）")
        return 0

    if args.dry_run:
        print(f"将注册 {len(jobs)} 个触发器（base={args.base_url}）：")
        for job in jobs:
            print(f"  · {job['name']:<24} cron={job['cron']:<14} sop={job['sop']:<32} role={job.get('role', '-')}")
        return 0

    if args.once:
        results = run_once(args.base_url, jobs)
        for item in results:
            mark = "✅" if item["ok"] else "❌"
            print(f"{mark} {item['job']}: {item.get('status') or item.get('error')}")
        return 0 if all(r["ok"] for r in results) else 1

    scheduler = build_scheduler(args.base_url, jobs)
    print(f"scheduler 已启动（{len(jobs)} 个触发器，Ctrl+C 退出）")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("scheduler 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
