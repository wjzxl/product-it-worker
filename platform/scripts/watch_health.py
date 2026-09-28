#!/usr/bin/env python3
"""平台自身的健康看门狗：**服务挂了要有人知道**。

为什么需要它：平台给业务系统做了告警接入（Alertmanager → 运维 SOP），但它自己挂掉时
没有任何人会收到通知——试点期间这是最尴尬的一种失败：AI 在等人审、审批台打不开，
而没人知道。这个脚本只做一件事：定期问每个服务的 `/health`，把"挂了/姿态不对/降级"
变成一条能发出去的通知。

检查四项（都来自 `/health`，不猜）：
  ① 可达且 200                     → 否则 critical（服务挂了）
  ② `status` 不是 ok               → warning（如知识库降级到内存桩）
  ③ `security.compliant=false` 且服务自称处于 staging/prod → **critical**（带着宽松开关跑生产）
  ④ `checkpointer.restart_safe=false` 且非 dev 档位 → critical（重启即丢等待中的人审）

通知三条路（可同时开）：
  · stdout（默认；cron/容器日志里就能看到）
  · `--notify-webhook`（飞书机器人 webhook，文本消息）
  · `--notify-task-center` + `--alert-secret`（把故障变成平台自己的运维任务——吃自己的狗粮；
    需要 task-center 自身活着，所以它是补充通道而不是唯一通道）

去重：状态写入 `--state-file`，**只在状态变化时通知**（含恢复通知），否则每分钟一条会把人吵到
把通知静音——那等于没有告警。

用法：
    python3 platform/scripts/watch_health.py --once
    python3 platform/scripts/watch_health.py --loop --interval 60 --notify-webhook "$FEISHU_URL"
    python3 platform/scripts/watch_health.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 本机一键起栈的端口表（与 run_local_stack.py 一致）；容器里用 WATCH_TARGETS 覆盖
DEFAULT_TARGETS = {
    "approval-gateway": "http://127.0.0.1:8001",
    "agent-runtime": "http://127.0.0.1:8002",
    "knowledge-base": "http://127.0.0.1:8003",
    "task-center": "http://127.0.0.1:8004",
    "metrics-dashboard": "http://127.0.0.1:8005",
    "gitlab-bridge": "http://127.0.0.1:8006",
    "process-mining": "http://127.0.0.1:8007",
}

STATE_FILE = Path(os.getenv("WATCH_STATE_FILE", "/tmp/platform-watch-health.json"))


def targets_from_env() -> dict[str, str]:
    """`WATCH_TARGETS="name=url,name=url"` 覆盖默认端口表（compose 里用内部服务名）。"""
    raw = os.getenv("WATCH_TARGETS", "").strip()
    if not raw:
        return dict(DEFAULT_TARGETS)
    out = {}
    for item in raw.split(","):
        if "=" in item:
            name, url = item.split("=", 1)
            out[name.strip()] = url.strip()
    return out or dict(DEFAULT_TARGETS)


def probe(name: str, url: str, timeout: float = 5.0) -> dict:
    """问一次 /health，返回 {name, url, level, reasons[]}。"""
    reasons: list[str] = []
    level = "ok"
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
            code = resp.status
    except urllib.error.HTTPError as exc:
        return {"name": name, "url": url, "level": "critical",
                "reasons": [f"HTTP {exc.code}"]}
    except Exception as exc:  # noqa: BLE001 —— 连不上就是连不上
        return {"name": name, "url": url, "level": "critical",
                "reasons": [f"{type(exc).__name__}: {exc}"]}

    if code != 200:
        reasons.append(f"HTTP {code}")
        level = "critical"

    status = str(body.get("status", "")).lower()
    if status and status != "ok":
        reasons.append(f"服务自报 status={status}")
        level = "warning" if level == "ok" else level

    sec = body.get("security") or {}
    strict = bool(sec.get("strict_required"))
    if sec.get("compliant") is False:
        if strict:
            reasons.append(f"生产/预发档位下治理开关不严：{sec.get('warnings')}")
            level = "critical"
        else:
            reasons.append(f"{sec.get('platform_env', 'dev')} 档位（宽松）：{len(sec.get('warnings') or [])} 项未开")

    cp = body.get("checkpointer") or {}
    if cp and cp.get("restart_safe") is False and strict:
        reasons.append(f"checkpointer 重启不安全：{cp.get('configured')}")
        level = "critical"

    if not reasons:
        reasons.append("正常")
    return {"name": name, "url": url, "level": level, "reasons": reasons}


def probe_all(targets: dict[str, str], timeout: float = 5.0) -> list[dict]:
    return [probe(name, url, timeout) for name, url in sorted(targets.items())]


def worst_level(results: list[dict]) -> str:
    for level in ("critical", "warning"):
        if any(r["level"] == level for r in results):
            return level
    return "ok"


def summarize(results: list[dict]) -> str:
    lines = []
    for r in results:
        mark = {"ok": "✅", "warning": "⚠️", "critical": "❌"}[r["level"]]
        lines.append(f"{mark} {r['name']}：{'；'.join(r['reasons'])}")
    return "\n".join(lines)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(path: Path, results: list[dict]) -> None:
    try:
        path.write_text(json.dumps({r["name"]: r["level"] for r in results}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def changed(results: list[dict], state: dict) -> tuple[list[dict], list[dict]]:
    """返回（新出问题的、恢复的）。只在变化时通知，避免刷屏导致告警被静音。"""
    worse = [r for r in results if r["level"] != "ok" and state.get(r["name"], "ok") == "ok"]
    recovered = [r for r in results if r["level"] == "ok" and state.get(r["name"], "ok") != "ok"]
    return worse, recovered


def post_json(url: str, payload: dict, timeout: float = 8.0, headers: dict | None = None) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        print(f"  （通知发送失败：{type(exc).__name__}: {exc}）", file=sys.stderr)
        return False


def notify(text: str, webhook: str = "", task_center: str = "", alert_secret: str = "",
           critical: bool = True) -> None:
    if webhook:
        # 飞书机器人文本消息格式
        ok = post_json(webhook, {"msg_type": "text", "content": {"text": text}})
        print(f"  → 已发飞书告警：{'成功' if ok else '失败'}")
    if task_center and critical:
        # 走平台自己的告警入口：平台故障 → 运维任务（与 Alertmanager 同一契约）
        payload = {"status": "firing", "alerts": [{
            "status": "firing",
            "labels": {"alertname": "PlatformHealthDegraded", "severity": "P1",
                       "job": "watch_health", "fingerprint": f"wh{int(time.time()) % 100000}"},
            "annotations": {"summary": text[:200], "description": text[:1000]},
        }]}
        ok = post_json(f"{task_center.rstrip('/')}/alerts/prometheus", payload,
                       headers={"X-Webhook-Token": alert_secret} if alert_secret else None)
        print(f"  → 已建运维任务：{'成功' if ok else '失败（task-center 可能也挂了，这正是需要外部通道的原因）'}")


def run_once(targets: dict[str, str], args) -> int:
    results = probe_all(targets, timeout=args.timeout)
    level = worst_level(results)
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] 平台健康：{level}")
    print(summarize(results))
    state = load_state(Path(args.state_file))
    worse, recovered = changed(results, state)
    if args.notify_always and level != "ok":
        notify(f"平台健康告警（{level}）\n{summarize(results)}", args.notify_webhook,
               args.notify_task_center, args.alert_secret)
    else:
        if worse:
            notify("平台健康告警：" + "；".join(f"{r['name']} {r['level']}" for r in worse)
                   + "\n" + summarize(worse), args.notify_webhook, args.notify_task_center,
                   args.alert_secret)
        if recovered:
            notify("平台已恢复：" + "、".join(r["name"] for r in recovered), args.notify_webhook,
                   "", "", critical=False)
    save_state(Path(args.state_file), results)
    return 1 if level == "critical" else 0


def selftest() -> int:
    """自检：用本地假服务造出"健康/降级/挂掉/生产姿态不严"四种情况，逐一验证判级与通知。"""
    import http.server
    import tempfile
    import threading

    checks: list[tuple[str, bool, str]] = []
    mode = {"value": "ok"}
    received: list[dict] = []

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if mode["value"] == "down":
                self.send_error(503, "down")
                return
            strict = mode["value"] == "prod_loose"
            body = {"status": "ok" if mode["value"] != "degraded" else "degraded",
                    "security": {"platform_env": "prod" if strict else "dev",
                                 "compliant": not strict or False,
                                 "strict_required": strict,
                                 "warnings": ["ENG_GATES_MODE=warn"] if strict else []}}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):  # noqa: D102
            pass

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # noqa: D102
            pass

    health_srv = http.server.HTTPServer(("127.0.0.1", 0), Health)
    sink_srv = http.server.HTTPServer(("127.0.0.1", 0), Sink)
    for srv in (health_srv, sink_srv):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{health_srv.server_port}"
    sink = f"http://127.0.0.1:{sink_srv.server_port}/hook"
    state = Path(tempfile.mkdtemp(prefix="watch-")) / "state.json"

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    try:
        mode["value"] = "ok"
        r = probe_all({"probe": url}, timeout=3)
        check("健康服务判为 ok", worst_level(r) == "ok", summarize(r))

        mode["value"] = "degraded"
        r = probe_all({"probe": url}, timeout=3)
        check("降级判为 warning", worst_level(r) == "warning", summarize(r))

        mode["value"] = "prod_loose"
        r = probe_all({"probe": url}, timeout=3)
        check("生产档位但治理开关不严 → critical", worst_level(r) == "critical", summarize(r))

        mode["value"] = "down"
        r = probe_all({"probe": url}, timeout=3)
        check("服务不可达 → critical", worst_level(r) == "critical", summarize(r))

        # 通知去重：同样的故障连续两次只通知一次；恢复时再通知一次
        # （刷屏的告警会被静音，等于没有告警——所以"只在状态变化时通知"本身要被测）
        mode["value"] = "down"
        notify_target = {"probe": url}
        worse1, rec1 = changed(probe_all(notify_target, timeout=3), {})
        worse2, rec2 = changed(probe_all(notify_target, timeout=3), {"probe": "critical"})
        check("同一故障只通知一次", len(worse1) == 1 and not rec1 and not worse2 and not rec2,
              f"首次 {len(worse1)} 条 / 再次 {len(worse2)} 条")
        mode["value"] = "ok"
        worse3, rec3 = changed(probe_all(notify_target, timeout=3), {"probe": "critical"})
        check("恢复会被识别并通知一次", len(rec3) == 1 and not worse3, f"{len(rec3)} 条恢复通知")

        notify("自检消息", webhook=sink)
        check("webhook 通知可送达", len(received) == 1 and received[0]["msg_type"] == "text",
              str(received)[:120])
    finally:
        health_srv.shutdown()
        sink_srv.shutdown()

    ok = all(c[1] for c in checks)
    for name, passed, detail in checks:
        print(f"{'✅' if passed else '❌'} {name}" + (f" — {detail}" if detail else ""))
    print("自检通过 ✅" if ok else "自检失败 ❌")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="检查一次（默认）")
    ap.add_argument("--loop", action="store_true", help="持续检查")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--notify-webhook", default=os.getenv("ALERT_NOTIFY_WEBHOOK", ""))
    ap.add_argument("--notify-task-center", default=os.getenv("TASK_CENTER_URL", ""))
    ap.add_argument("--alert-secret", default=os.getenv("ALERT_WEBHOOK_SECRET", "")
                    or os.getenv("WEBHOOK_SECRET", ""))
    ap.add_argument("--notify-always", action="store_true", help="每次异常都通知（默认只在状态变化时）")
    ap.add_argument("--state-file", default=str(STATE_FILE))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    targets = targets_from_env()
    if args.loop:
        print(f"看门狗启动：{len(targets)} 个目标，每 {args.interval}s 检查一次")
        while True:
            run_once(targets, args)
            time.sleep(args.interval)
    return run_once(targets, args)


if __name__ == "__main__":
    sys.exit(main())
