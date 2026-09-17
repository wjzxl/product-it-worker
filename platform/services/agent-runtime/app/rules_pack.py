"""项目规则包加载（WP4）：把规则"喂进"Agent，而不是只放在仓库里。

对应课程图 22：Harness 是**能打开的文件夹**——项目入口告诉 AI 先看什么，规则分目录存放。
对应计划 WP4 验收③：**规则包必须被 Agent 实际引用**（prompt 指纹可查），否则就是假合规。

设计：
- 只注入**摘要 + 约束要点**（不是把几十份文件全塞进 prompt，那会吃掉上下文且降低遵从度）；
- 指纹（sha256 前 16 位）随规则内容变化，可在 `/rules-pack` 与 prompt 元数据里核对；
- `PROJECT_RULES_DIR` 可指向其它项目的规则包（多项目复用，课程图 45）。
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "project-rules"
_LAYERS = ("constraints", "design", "completeness")


def rules_dir() -> Path:
    env = os.getenv("PROJECT_RULES_DIR", "")
    return Path(env) if env else DEFAULT_DIR


def _read(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # 去掉引用块与空行，压掉细节，只留可执行要点
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(">")]
    return "\n".join(lines)[:limit]


def load_pack(directory: Optional[Path] = None) -> dict:
    """加载规则包：返回 {name, version, layers{layer:[{path, digest, summary}]}, fingerprint}。"""
    directory = directory or rules_dir()
    manifest_path = directory / "manifest.yaml"
    if not manifest_path.exists():
        logger.warning("未找到项目规则包：%s（Agent 将不带项目规则运行）", manifest_path)
        return {"name": "", "version": None, "layers": {}, "fingerprint": "", "available": False}
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    layers: dict[str, list[dict]] = {}
    for layer in _LAYERS:
        spec = (manifest.get("layers") or {}).get(layer) or {}
        entries = []
        for item in spec.get("files") or []:
            path = directory / str(item.get("path") or "")
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            entries.append({
                "path": str(item.get("path")),
                "title": spec.get("title", layer),
                "digest": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                "summary": _read(path),
            })
        layers[layer] = entries

    digest_src = "|".join(
        f"{e['path']}:{e['digest']}" for layer in _LAYERS for e in layers.get(layer, [])
    )
    return {
        "name": manifest.get("name", ""),
        "version": manifest.get("version"),
        "layers": layers,
        "fingerprint": hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16] if digest_src else "",
        "available": bool(digest_src),
        "dir": str(directory),
    }


def prompt_excerpt(pack: Optional[dict] = None, per_layer_chars: int = 700) -> str:
    """给 Agent 用的规则摘要（带指纹，便于核查"这轮用的是哪版规则"）。"""
    pack = pack or load_pack()
    if not pack.get("available"):
        return ""
    lines = [f"# 项目规则包（{pack['name']} v{pack['version']}｜指纹 {pack['fingerprint']}）",
             "以下规则来自项目规则库，**必须遵守**；与你的通用习惯冲突时以本规则为准。"]
    for layer in _LAYERS:
        entries = pack["layers"].get(layer) or []
        if not entries:
            continue
        lines.append(f"\n## {layer}")
        for entry in entries:
            lines.append(f"### {entry['path']}（digest {entry['digest']}）")
            lines.append(entry["summary"][:per_layer_chars])
    return "\n".join(lines)


def stats(pack: Optional[dict] = None) -> dict:
    pack = pack or load_pack()
    return {
        "available": pack.get("available", False),
        "name": pack.get("name", ""),
        "version": pack.get("version"),
        "fingerprint": pack.get("fingerprint", ""),
        "dir": pack.get("dir", ""),
        "files": {layer: [e["path"] for e in pack["layers"].get(layer, [])] for layer in _LAYERS},
        "counts": {layer: len(pack["layers"].get(layer, [])) for layer in _LAYERS},
    }
