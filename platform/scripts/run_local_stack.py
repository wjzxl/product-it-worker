#!/usr/bin/env python3
"""本地/单机启动全栈（为"跑一条真实需求"准备）。

为什么需要它：平台有 7 个服务，各自 venv 与端口；要收集闸门真实样本，
必须把 approval-gateway / task-center / agent-runtime / knowledge-base 同时起起来，
手工拼环境变量容易漏（漏了 ENG_GATES_MODE 就退回默认，样本口径就变了）。

本脚本做三件事：
  1. 按固定端口启动所需服务（默认只起跑真实需求必需的四件套 + 可选调度器）；
  2. 显式注入闸门/持久化/知识库等环境变量，并把**生效的关键配置打印出来**；
  3. 等待健康检查通过后打印入口 URL（驾驶舱 / 审批台 / 各服务 /docs），随后常驻。

用法：
    cd platform
    # 默认：warn 灰度（推荐，先收样本）
    services/agent-runtime/.venv/bin/python scripts/run_local_stack.py
    # 只起核心三件（不含知识库，Runbook RAG 不可用）
    ... --services approval-gateway,agent-runtime,task-center
    # 额外起定时触发器（运维晨检 7:30）
    ... --with-scheduler
    # 检查环境与端口但不启动
    ... --dry-run
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = ROOT / "services"

PORTS = {
    "approval-gateway": 8001,
    "agent-runtime": 8002,
    "knowledge-base": 8003,
    "task-center": 8004,
    "metrics-dashboard": 8005,
    "gitlab-bridge": 8006,
    "process-mining": 8007,
}
DEFAULT_SERVICES = ("approval-gateway", "agent-runtime", "knowledge-base", "task-center", "metrics-dashboard")

PG_DSN = "postgresql://platform:platform@127.0.0.1:5432/platform"
PG_URL = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform"


def python_of(service: str) -> Path:
    return SERVICES_DIR / service / ".venv" / "bin" / "python"


def base_env(args) -> dict:
    """统一注入环境（闸门口径、持久化、知识库、跨服务地址）。"""
    env = dict(os.environ)
    env.update({
        # 工程闸门：默认 warn —— 先收真实样本，别一上来就 block
        "ENG_GATES_MODE": args.gates_mode,
        "ENG_GATES_REQUIRED": os.getenv("ENG_GATES_REQUIRED", "0"),
        # 运行状态持久化（等待/唤醒依赖它）
        "CHECKPOINTER": args.checkpointer,
        "CHECKPOINTER_DSN": PG_DSN,
        # 服务间地址
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "AGENT_RUNTIME_URL": f"http://127.0.0.1:{PORTS['agent-runtime']}",
        "KNOWLEDGE_BASE_URL": f"http://127.0.0.1:{PORTS['knowledge-base']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['gitlab-bridge']}",
        # 资产目录（岗位三件套 / 规则包 / 工具注册表 / 闸门规则）
        "PROMPTS_DIR": str(ROOT / "prompts"),
        "PROJECT_RULES_DIR": str(ROOT / "project-rules"),
        "ROLES_DIR": str(ROOT / "roles"),
        "TOOLS_DIR": str(ROOT / "tools"),
        "ENGINEERING_GATES_DIR": str(ROOT / "engineering-gates"),
        "AMBIGUITY_TUNABLES": str(ROOT / "ambiguity-tunables.yaml"),
        "SOP_CONFIG_PATH": str(ROOT / "sop-config.yaml"),
        # 归档与证据目录
        "RUNS_DB_URL": os.getenv("RUNS_DB_URL", PG_URL),
        "EVIDENCE_DIR": os.getenv("EVIDENCE_DIR", str(ROOT / "evidence")),
        "DEFAULT_TENANT": os.getenv("DEFAULT_TENANT", "default"),
    })
    # 数据库：approval-gateway / task-center 用同一个平台库
    env["DATABASE_URL"] = os.getenv("DATABASE_URL", PG_URL)
    # 知识库（有 Key 才走真实向量，否则用哈希桩——便于无 Key 环境跑通）
    env.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
    env.setdefault("KB_COLLECTION", "platform_kb_qwen")
    env.setdefault("EMBEDDING_MODEL", "qwen-embed")
    env.setdefault("LLM_BASE_URL", "http://127.0.0.1:4000/v1")
    if os.getenv("LLM_API_KEY") or os.getenv("LITELLM_MASTER_KEY"):
        env.setdefault("LLM_API_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-litellm-master"))
    return env


def show_config(env: dict, args) -> None:
    interesting = ("ENG_GATES_MODE", "ENG_GATES_REQUIRED", "CHECKPOINTER", "DEFAULT_TENANT",
                   "DATABASE_URL", "RUNS_DB_URL", "KB_COLLECTION", "EMBEDDING_MODEL")
    print("生效配置：")
    for key in interesting:
        value = env.get(key, "")
        if key.endswith("URL") and "@" in value:
            value = value.split("@")[-1]
        print(f"  {key}={value or '（未设置）'}")
    print(f"  LLM_API_KEY={'已设置' if env.get('LLM_API_KEY') else '未设置（将用模板桩，无真实模型调用）'}")


def check_ports(services: list[str]) -> list[str]:
    import socket

    busy = []
    for name in services:
        port = PORTS[name]
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(f"{name}(:{port})")
    return busy


def wait_healthy(services: list[str], timeout: int = 90) -> dict[str, bool]:
    deadline = time.time() + timeout
    status = {name: False for name in services}
    while time.time() < deadline and not all(status.values()):
        for name in services:
            if status[name]:
                continue
            try:
                if httpx.get(f"http://127.0.0.1:{PORTS[name]}/health", timeout=2).status_code == 200:
                    status[name] = True
            except Exception:
                pass
        time.sleep(1.5)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="本地启动全栈（为跑真实需求准备）")
    parser.add_argument("--services", default=",".join(DEFAULT_SERVICES))
    parser.add_argument("--gates-mode", default=os.getenv("ENG_GATES_MODE", "warn"),
                        choices=["off", "warn", "block"])
    parser.add_argument("--checkpointer", default=os.getenv("CHECKPOINTER", "postgres"),
                        choices=["memory", "postgres"])
    parser.add_argument("--with-scheduler", action="store_true", help="额外启动定时触发器进程")
    parser.add_argument("--dry-run", action="store_true", help="只打印将启动的服务与环境，不实际启动")
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()]
    unknown = [s for s in services if s not in PORTS]
    if unknown:
        print(f"❌ 未知服务：{unknown}（可选：{list(PORTS)}）", file=sys.stderr)
        return 2
    missing_venv = [s for s in services if not python_of(s).exists()]
    if missing_venv:
        print(f"❌ 缺少 venv：{missing_venv}（先在该服务目录 pip install -e '.[dev]'）", file=sys.stderr)
        return 2

    env = base_env(args)
    show_config(env, args)
    print(f"\n将启动：{', '.join(f'{s}:{PORTS[s]}' for s in services)}"
          + (" + scheduler" if args.with_scheduler else ""))

    busy = check_ports(services)
    if busy:
        print(f"❌ 端口已被占用：{busy}（先停掉已有实例，避免口径混乱）", file=sys.stderr)
        return 2
    if args.dry_run:
        print("（dry-run：未启动）")
        return 0

    procs: list[tuple[str, subprocess.Popen]] = []
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    for name in services:
        log = open(log_dir / f"{name}.log", "w")
        procs.append((name, subprocess.Popen(
            [str(python_of(name)), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORTS[name])],
            cwd=SERVICES_DIR / name, env=env, stdout=log, stderr=subprocess.STDOUT,
        )))
    if args.with_scheduler:
        log = open(log_dir / "scheduler.log", "w")
        procs.append(("scheduler", subprocess.Popen(
            [str(python_of("agent-runtime")), "-m", "app.scheduler", "--base-url", env["AGENT_RUNTIME_URL"]],
            cwd=SERVICES_DIR / "agent-runtime", env=env, stdout=log, stderr=subprocess.STDOUT,
        )))

    print("\n等待健康检查…")
    status = wait_healthy(services)
    for name, ok in status.items():
        print(f"  {'✅' if ok else '❌'} {name} (: {PORTS[name]})")
    if not all(status.values()):
        print(f"\n⚠️ 有服务未就绪，日志见 {log_dir}/（常见原因：PG/Qdrant/LiteLLM 未启动）", file=sys.stderr)

    print(f"""
入口：
  驾驶舱（待决策队列）  http://127.0.0.1:{PORTS['metrics-dashboard']}/
  审批台               http://127.0.0.1:{PORTS['approval-gateway']}/  （API 文档 /docs）
  闸门规则             http://127.0.0.1:{PORTS['approval-gateway']}/eng-gates
  岗位注册表           http://127.0.0.1:{PORTS['agent-runtime']}/roles
  工具注册表           http://127.0.0.1:{PORTS['agent-runtime']}/tools
  运行归档             http://127.0.0.1:{PORTS['agent-runtime']}/runs
  指标                 http://127.0.0.1:{PORTS['metrics-dashboard']}/metrics

下一步（跑一条真实需求）：
  curl -s -X POST http://127.0.0.1:{PORTS['agent-runtime']}/runs -H 'Content-Type: application/json' -d '{{
    "requirement_text": "<把你们真实需求粘进来>",
    "task_ref": "real-001",
    "trigger": "manual",
    "sources": [{{"kind": "chat", "ref": "<群消息链接>", "quote": "<原话>", "confirmed_by": "<谁说的>"}}]
  }}' | python3 -m json.tool

  跑完后看样本与就绪度：
    services/approval-gateway/.venv/bin/python scripts/gate_metrics.py
    services/approval-gateway/.venv/bin/python scripts/gate_readiness.py

按 Ctrl+C 停止全部服务。
""")

    try:
        while True:
            time.sleep(1)
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"⚠️ {name} 已退出（code={proc.returncode}），日志：{log_dir}/{name}.log", file=sys.stderr)
                    procs = [(n, p) for n, p in procs if n != name]
            if not procs:
                print("所有服务已退出。", file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        for name, proc in procs:
            proc.terminate()
        time.sleep(1)
        for name, proc in procs:
            if proc.poll() is None:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
