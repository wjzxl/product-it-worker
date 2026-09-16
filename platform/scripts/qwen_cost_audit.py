#!/usr/bin/env python3
"""Qwen 成本计量审计（只读，不修改任何计费日志）。

背景：平台早期用 `openai/` 兼容路由调 Qwen，LiteLLM 不认识模型名 → 响应不带成本 →
`LiteLLM_SpendLogs.spend` 全部记 0，面板累计成本被低估。修复（显式单价）**只对之后的调用生效**，
历史行不会被回填，所以需要这样一个审计把缺口量化出来。

本脚本做三件事：
1. 从 `litellm-config.yaml` 读取各模型的**单价**（单一事实来源，避免两处硬编码漂移）；
2. 按模型汇总 spend 日志里的调用数 / tokens / 已记录花费；
3. 用 tokens × 单价算出**应有花费**，给出缺口（未计量部分）与全平台口径修正值。

只读：仅执行 SELECT。若要把估算写回计费库，请先与账单核对后自行决定，本脚本不代劳。

用法：
    cd platform
    services/metrics-dashboard/.venv/bin/python scripts/qwen_cost_audit.py
    ... --json          # 机器可读输出
    ... --dsn "host=... dbname=litellm user=platform password=platform"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(os.getenv("LITELLM_CONFIG", ROOT / "litellm-config.yaml"))
DEFAULT_DSN = os.getenv(
    "LITELLM_DSN", "host=127.0.0.1 port=5432 dbname=litellm user=platform password=platform"
)

# 需要审计的模型前缀（默认只看 Qwen 系；--all 覆盖全部模型）
QWEN_MARKERS = ("qwen", "text-embedding-v")


def load_prices(config_path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """返回 (价格表, 别名→底层模型名)。

    价格表键同时包含底层模型名（spend 日志里记录的就是它，如 `openai/qwen-max`）
    与配置别名（如 `qwen-max`），两者都能命中。
    """
    doc = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    prices: dict[str, dict] = {}
    alias_to_model: dict[str, str] = {}
    for entry in doc.get("model_list") or []:
        alias = entry.get("model_name") or ""
        params = entry.get("litellm_params") or {}
        underlying = params.get("model") or alias
        info = entry.get("model_info") or {}
        price = {
            "model": underlying,
            "alias": alias,
            "input_cost_per_token": info.get("input_cost_per_token"),
            "output_cost_per_token": info.get("output_cost_per_token"),
            "base_model": info.get("base_model"),
        }
        if price["input_cost_per_token"] is None and price["output_cost_per_token"] is None:
            continue  # 无显式价格：走 LiteLLM 内置价格表，不参与审计（其计量本身正常）
        prices[underlying] = price
        prices[alias] = price
        alias_to_model[alias] = underlying
    return prices, alias_to_model


def audit(dsn: str, prices: dict[str, dict], all_models: bool = False) -> dict:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            'select model, count(*), coalesce(sum(spend), 0), '
            'coalesce(sum(prompt_tokens), 0), coalesce(sum(completion_tokens), 0), '
            'coalesce(sum("total_tokens"), 0) '
            'from "LiteLLM_SpendLogs" group by model order by 2 desc'
        ).fetchall()

    items, total_recorded, total_expected = [], 0.0, 0.0
    for model, calls, recorded, prompt_tokens, completion_tokens, total_tokens in rows:
        price = prices.get(model)
        if price is None and not all_models:
            continue
        recorded = float(recorded or 0)
        total_recorded += recorded
        if price is None:  # --all 下未配置显式价格的模型：计量交给 LiteLLM 内置价格表
            items.append(
                {
                    "model": model,
                    "calls": calls,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "recorded_usd": round(recorded, 8),
                    "expected_usd": None,
                    "gap_usd": None,
                    "priced": False,
                }
            )
            continue
        in_cost = price["input_cost_per_token"] or 0.0
        out_cost = price["output_cost_per_token"] or 0.0
        expected = prompt_tokens * in_cost + completion_tokens * out_cost
        total_expected += expected
        items.append(
            {
                "model": model,
                "alias": price["alias"],
                "calls": calls,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "unit_price_in": in_cost,
                "unit_price_out": out_cost,
                "recorded_usd": round(recorded, 8),
                "expected_usd": round(expected, 8),
                "gap_usd": round(max(expected - recorded, 0.0), 8),
                "priced": True,
            }
        )
    gap = sum(i["gap_usd"] or 0.0 for i in items)
    return {
        "items": items,
        "recorded_usd": round(total_recorded, 6),
        "expected_usd": round(total_expected, 6),
        "unmetered_gap_usd": round(gap, 6),
    }


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen/embedding 成本计量审计（只读）")
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--all", action="store_true", help="审计全部模型（默认只审计 Qwen 系）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"❌ 找不到 LiteLLM 配置：{config_path}", file=sys.stderr)
        return 2
    prices, _ = load_prices(config_path)
    if not prices:
        print("❌ 配置里没有带显式单价的模型（model_info.input_cost_per_token）", file=sys.stderr)
        return 2

    try:
        result = audit(args.dsn, prices, all_models=args.all)
    except psycopg.Error as exc:
        print(f"❌ 读取 LiteLLM 库失败：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"单价来源：{config_path.relative_to(ROOT)}（model_info 显式单价）")
    print(f"计费库：{args.dsn.split('dbname=')[-1].split()[0]}\n")
    header = f"{'模型':<28}{'调用':>6}{'in tok':>9}{'out tok':>9}{'已记录$':>13}{'应有$':>13}{'缺口$':>13}"
    print(header)
    print("-" * len(header))
    for i in result["items"]:
        if not i["priced"]:
            print(f"{i['model']:<28}{i['calls']:>6}{i['prompt_tokens']:>9}{i['completion_tokens']:>9}"
                  f"{_fmt(i['recorded_usd']):>13}{'内置价格表':>13}{'—':>13}")
            continue
        print(
            f"{i['model']:<28}{i['calls']:>6}{i['prompt_tokens']:>9}{i['completion_tokens']:>9}"
            f"{_fmt(i['recorded_usd']):>13}{_fmt(i['expected_usd']):>13}{_fmt(i['gap_usd']):>13}"
        )
    print("-" * len(header))
    print(f"已记录合计 ${result['recorded_usd']}｜应有合计 ${result['expected_usd']}｜"
          f"未计量缺口 ${result['unmetered_gap_usd']}")
    print("\n说明：tokens 取自计费日志（实测值），单价取自配置（显式声明），因此“应有花费”是可复核的估算；")
    print("      缺口来自早期用 openai/ 兼容路由调用 Qwen 时 LiteLLM 无法计价——修复只对之后的调用生效，不回填历史。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
