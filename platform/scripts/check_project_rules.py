#!/usr/bin/env python3
"""项目规则包校验器（WP4）：保证规则"能被读到、能被判定、不悄悄消失"。

校验项（计划 WP4 验收）：
  1. `manifest.yaml` 合法：三层齐全、每层文件都登记了 required_sections；
  2. **声明的文件必须存在**；每份文件的必备小节必须出现（`## 小节名`）；
  3. **目录里多出的规则文件必须登记**——防止"写了规则但没人读"（假合规）；
  4. 五份设计模板齐备（data/business/api/frontend/test）；
  5. `not_applicable` 必须带 reason（在用到它的地方：模板/闸门上下文由 E2 校验，这里查模板说明）。

退出码非 0 即失败，可直接进 CI。

用法：
    cd platform
    services/approval-gateway/.venv/bin/python scripts/check_project_rules.py
    ... --dir /path/to/project-rules     # 校验别的项目的规则包
    ... --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "project-rules"
LAYERS = ("constraints", "design", "completeness")
FACETS = ("data", "business", "api", "frontend", "test")


class RulePackError(ValueError):
    """规则包非法（报错要指明文件与原因）。"""


def _sections(text: str) -> list[str]:
    """抽取 `## 标题` 形式的小节名。"""
    return [m.group(1).strip() for m in re.finditer(r"^#{1,3}\s+(.+)$", text, re.MULTILINE)]


def load_manifest(directory: Path) -> dict:
    path = directory / "manifest.yaml"
    if not path.exists():
        raise RulePackError(f"缺少 manifest.yaml：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RulePackError(f"manifest.yaml 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise RulePackError("manifest.yaml 顶层必须是对象")
    return data


def validate(directory: Path) -> dict:
    """返回 {ok, errors[], warnings[], stats{}}。errors 非空即失败。"""
    errors: list[str] = []
    warnings: list[str] = []
    manifest = load_manifest(directory)
    layers = manifest.get("layers") or {}
    declared: set[str] = set()

    for layer in LAYERS:
        spec = layers.get(layer)
        if not spec:
            errors.append(f"manifest 缺少层：{layer}")
            continue
        files = spec.get("files") or []
        if not files:
            errors.append(f"{layer} 层没有任何文件登记")
        for item in files:
            rel = str(item.get("path") or "")
            if not rel:
                errors.append(f"{layer} 层存在未写 path 的条目")
                continue
            declared.add(f"{layer}/{rel.split('/', 1)[-1]}")
            path = directory / rel
            if not path.exists():
                errors.append(f"{layer} 声明的文件不存在：{rel}")
                continue
            text = path.read_text(encoding="utf-8")
            present = _sections(text)
            for section in item.get("required_sections") or []:
                if not any(section in s for s in present):
                    errors.append(f"{rel} 缺少必备小节：{section}")

    # 目录里多出的文件必须登记（防"写了规则没人读"）
    for layer in LAYERS:
        layer_dir = directory / layer
        if not layer_dir.exists():
            errors.append(f"缺少层目录：{layer}/")
            continue
        for path in sorted(layer_dir.glob("*.md")):
            rel = f"{layer}/{path.name}"
            if rel not in declared:
                errors.append(f"{rel} 存在但未在 manifest 登记（写了规则却没人读=假合规）")

    # 模板与不适用声明
    templates = manifest.get("templates") or {}
    tdir = directory / str(templates.get("dir") or "templates")
    facets = templates.get("facets") or list(FACETS)
    if set(facets) != set(FACETS):
        errors.append(f"templates.facets 必须恰好是 {list(FACETS)}，收到 {facets}")
    for facet in FACETS:
        rel = (templates.get("files") or {}).get(facet) or f"templates/{facet}.md"
        path = directory / rel
        if not path.exists():
            errors.append(f"缺少 {facet} 设计模板：{rel}")
    # 不适用声明必须写原因：模板里要出现 not_applicable 的用法说明
    na_doc = directory / "design" / "five-design-docs.md"
    if na_doc.exists() and "not_applicable" not in na_doc.read_text(encoding="utf-8"):
        errors.append("design/five-design-docs.md 未说明 not_applicable 的用法（缺原因声明会退化成漏设计）")

    stats = {
        "layers": {layer: len((layers.get(layer) or {}).get("files") or []) for layer in LAYERS},
        "declared_files": len(declared),
        "templates": len(FACETS),
        "name": manifest.get("name", ""),
        "version": manifest.get("version"),
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


def main() -> int:
    parser = argparse.ArgumentParser(description="项目规则包校验（WP4）")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    directory = Path(args.dir)
    try:
        result = validate(directory)
    except RulePackError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        stats = result["stats"]
        print(f"规则包：{stats['name']} v{stats['version']}（{directory}）")
        for layer, count in stats["layers"].items():
            print(f"  {layer}: {count} 份")
        print(f"  模板：{stats['templates']} 份（五份配套设计）")
        if result["errors"]:
            print(f"\n❌ 校验未通过（{len(result['errors'])} 项）：")
            for err in result["errors"]:
                print(f"  - {err}")
        else:
            print("\n✅ 规则包校验通过")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
