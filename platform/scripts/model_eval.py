#!/usr/bin/env python3
"""多模型 / 私有化模型评估 CLI（Phase 3 ③）。

用法：
  # 对比两个云模型（走 LiteLLM 网关，默认 http://127.0.0.1:4000/v1）
  cd platform && services/agent-runtime/.venv/bin/python scripts/model_eval.py \
      --models deepseek-chat,qwen-plus --tag cloud

  # 只跑 2 个任务做冒烟
  ... --models deepseek-chat --limit 2

  # 评估私有化/自建端点（vLLM / Ollama / 内网部署，任意 OpenAI 兼容）
  EVAL_BASE_URL=http://10.0.0.9:8000/v1 EVAL_API_KEY=none \
  services/agent-runtime/.venv/bin/python scripts/model_eval.py --models qwen2.5-14b-instruct --tag private

  # 沙箱判分用 docker 隔离（推荐）
  SANDBOX_MODE=docker ... python scripts/model_eval.py --models deepseek-chat
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))

from app.eval import GoldenError, HttpLLM, load_golden, run_eval, write_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="模型评估（金标集 + 沙箱判分）")
    parser.add_argument("--models", default=os.getenv("EVAL_MODELS", "deepseek-chat"),
                        help="逗号分隔的模型名；私有化端点用 EVAL_MODEL 或此参数")
    parser.add_argument("--golden", default=str(ROOT / "eval" / "golden"))
    parser.add_argument("--out", default=str(ROOT / "reports"))
    parser.add_argument("--tag", default="", help="报告文件名标签（如 cloud / private / qwen）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务（冒烟用）")
    parser.add_argument("--mode", choices=["gateway", "private", "auto"], default="auto",
                        help="gateway=LiteLLM 网关；private=EVAL_BASE_URL 指向的私有化端点")
    args = parser.parse_args()

    if args.mode == "private" and not os.getenv("EVAL_BASE_URL"):
        print("❌ --mode private 需要设置 EVAL_BASE_URL（私有化端点的 /v1 地址）", file=sys.stderr)
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("❌ 未指定模型", file=sys.stderr)
        return 2

    try:
        tasks = load_golden(args.golden)
    except GoldenError as exc:
        print(f"❌ 金标集校验失败：{exc}", file=sys.stderr)
        return 2

    endpoint = os.getenv("EVAL_BASE_URL", "http://127.0.0.1:4000/v1")
    print(f"金标集 {len(tasks)} 个任务（{', '.join(t.role for t in tasks)}）")
    print(f"端点 {endpoint} | 沙箱 {os.getenv('SANDBOX_MODE', 'host')} | 模型 {', '.join(models)}")
    if args.limit:
        print(f"冒烟模式：只跑前 {args.limit} 个任务")

    def factory(model: str) -> HttpLLM:
        return HttpLLM.from_env(model)

    report = run_eval(tasks, models, llm_factory=factory, limit=args.limit or None)
    md_path, json_path = write_report(args.out, report, tag=args.tag)

    print("\n== 模型横向对比 ==")
    for row in report["models"]:
        if row.get("error"):
            print(f"  {row['model']}: 初始化失败 {row['error']}")
            continue
        cost = "unmetered" if row["cost_usd"] is None else f"${row['cost_usd']:.6f}"
        print(f"  {row['model']}: 通过 {row['passed']}/{row['tasks']} "
              f"(加权 {row['avg_score']:.3f}) | P50 {row['latency_p50_ms']:.0f}ms "
              f"| {row['total_tokens']} tokens | {cost}")
    failures = [r for r in report["results"] if not r.get("passed")]
    if failures:
        print(f"\n== 未通过 {len(failures)} 项（详见报告第 3 节）==")
        for row in failures[:6]:
            reason = row.get("error") or "；".join(
                f"{c['label']}:{c['detail']}" for c in row.get("checks", []) if not c.get("passed")
            )
            print(f"  [{row['model']}] {row['task_id']}: {reason[:140]}")
    print(f"\n报告：{md_path.relative_to(ROOT)}")
    print(f"原始数据：{json_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
