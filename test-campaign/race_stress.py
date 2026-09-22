#!/usr/bin/env python3
"""并发压测：把"竞态"从概率事件变成可复现证据。

`probes-concurrency.sh` 里的 2–8 并发探针只能说明"这一次没撞上"，撞不上不等于没有竞态。
本脚本把并发拉到可配置的量级（默认 30 线程 × 8 轮），并直接把数据库行数拉出来对账：

- `--target approval`：同一审批单并发决策 —— 必须恰好 1 个 HTTP 200、恰好 1 条决策行。
- `--target task`    ：同一任务并发流转 —— 必须恰好 1 个 HTTP 200、恰好 1 条新的
  status_changed 时间线（基线 3 条：create/提交 running/waiting_approval）。

用法（需 SUT 跑 Postgres 后端：`bash test-campaign/start-sut.sh --backend postgres`）：
    python3 test-campaign/race_stress.py --target approval --concurrency 30 --rounds 8
    python3 test-campaign/race_stress.py --target task --concurrency 30 --rounds 6

退出码：0 = 全部轮次通过；1 = 至少一轮出现重复写入（真缺陷）；2 = 环境不满足
（连不上 SUT 或拿不到数据库行数——后者需要 docker exec platform-pg，缺失时报错而不是静默放过）。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_GW = "http://127.0.0.1:18001"
DEFAULT_TC = "http://127.0.0.1:18004"
TOKEN = "pst-sut-token"


def _post(url: str, body: dict, token: str, tenant: str = "") -> int:
    headers = {"Content-Type": "application/json", "X-Service-Token": token}
    if tenant:
        headers["X-Tenant"] = tenant
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 —— 连接失败也算一次失败请求，由调用方判定
        return 0


def psql(sql: str, db: str = "pst_sut") -> str:
    proc = subprocess.run(
        ["docker", "exec", "platform-pg", "psql", "-U", "platform", "-d", db, "-tAc", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"无法读取数据库行数（需要 docker exec platform-pg）：{proc.stderr.strip()}")
    return proc.stdout.strip()


def _mk_approval(gw: str, ref: str, token: str) -> str:
    deadline = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
    body = {
        "type": "GATE_REQUIREMENT_REVIEW", "stage": "stress", "requester": "campaign", "task_ref": ref,
        "context_snapshot": {"artifact_ref": ref, "diff_summary": "stress", "evidence": ["http://e/1"],
                             "risk": {"score": 20, "level": "L2", "factors": ["stress"]}},
        "deadline": deadline,
        "escalation_policy": {"remind_after_hours": 1, "escalate_to": "lead", "timeout_action": "reject"},
    }
    req = urllib.request.Request(f"{gw}/approvals", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Service-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["id"]


def run_approval(gw: str, token: str, n: int, rounds: int) -> int:
    bad = 0
    for rnd in range(rounds):
        ref = f"STRESS-A-{rnd}"
        aid = _mk_approval(gw, ref, token)
        before = int(psql(f"select count(*) from approval_decisions where request_id='{aid}'"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            codes = [f.result() for f in
                     [ex.submit(_post, f"{gw}/approvals/{aid}/decision",
                                {"decision": "approve", "decided_by": f"racer-{i}", "comment": "stress"},
                                token) for i in range(n)]]
        after = int(psql(f"select count(*) from approval_decisions where request_id='{aid}'"))
        n200, rows = codes.count(200), after - before
        ok = n200 == 1 and rows == 1
        bad += 0 if ok else 1
        print(f"  round {rnd}: 并发={n} HTTP200={n200} 新增决策行={rows} "
              f"{'OK' if ok else 'RACE'} 状态码分布={ {c: codes.count(c) for c in sorted(set(codes))} }")
    return bad


def run_task(tc: str, token: str, n: int, rounds: int) -> int:
    bad = 0
    for rnd in range(rounds):
        req = urllib.request.Request(f"{tc}/tasks", data=json.dumps({"sop": "dev-domain/issue-to-merge"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Tenant": "stress",
                                              "X-Service-Token": token})
        with urllib.request.urlopen(req, timeout=30) as r:
            tid = json.load(r)["id"]
        _post(f"{tc}/tasks/{tid}/transition", {"to": "running", "by": "agent"}, token, "stress")
        _post(f"{tc}/tasks/{tid}/transition", {"to": "waiting_approval", "by": "agent"}, token, "stress")
        before = int(psql(f"select count(*) from task_events where task_id='{tid}' and event_type='status_changed'"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            codes = [f.result() for f in
                     [ex.submit(_post, f"{tc}/tasks/{tid}/transition",
                                {"to": "running", "by": f"h{i}",
                                 "detail": {"approval_id": "apv_stress", "decided_by": "racer"}}, token, "stress")
                      for i in range(n)]]
        after = int(psql(f"select count(*) from task_events where task_id='{tid}' and event_type='status_changed'"))
        n200, rows = codes.count(200), after - before
        ok = n200 == 1 and rows == 1
        bad += 0 if ok else 1
        print(f"  round {rnd}: 并发={n} HTTP200={n200} 新增流转事件={rows} {'OK' if ok else 'RACE'}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["approval", "task"], required=True)
    ap.add_argument("--concurrency", type=int, default=30)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--gateway", default=DEFAULT_GW)
    ap.add_argument("--task-center", default=DEFAULT_TC)
    ap.add_argument("--token", default=TOKEN)
    args = ap.parse_args()

    try:
        if args.target == "approval":
            bad = run_approval(args.gateway, args.token, args.concurrency, args.rounds)
            label = "审批决策"
        else:
            bad = run_task(args.task_center, args.token, args.concurrency, args.rounds)
            label = "任务流转"
    except RuntimeError as exc:
        print(f"INVALID: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"INVALID: {type(exc).__name__}: {exc}（SUT 是否已启动？）")
        return 2

    if bad:
        print(f"FINDING: {label}在 {bad}/{args.rounds} 轮出现重复写入（并发未串行化）")
        return 1
    print(f"RESULT: PASS {label}在 {args.rounds} 轮 × {args.concurrency} 并发下均恰好成功一次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
