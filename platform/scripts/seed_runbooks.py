#!/usr/bin/env python3
"""Runbook 入库：把 platform/runbooks/*.md 写入知识库（Qwen 向量，供运维域诊断 RAG 检索）。

用法：
  export LLM_API_KEY=sk-litellm-master LLM_BASE_URL=http://localhost:4000/v1 \
         EMBEDDING_MODEL=qwen-embed EMBEDDING_DIM=1024 QDRANT_URL=http://localhost:6333
  services/knowledge-base/.venv/bin/python scripts/seed_runbooks.py
"""
from __future__ import annotations

import glob
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_URL = os.getenv("KB_URL", "http://localhost:8004")


def main() -> int:
    files = sorted(glob.glob(os.path.join(ROOT, "runbooks", "*.md")))
    if not files:
        print("未找到 runbooks/*.md")
        return 1
    ok = 0
    for path in files:
        source = os.path.basename(path)
        content = open(path, encoding="utf-8").read()
        try:
            r = httpx.post(f"{KB_URL}/documents", json={"source": source, "content": content}, timeout=120)
            r.raise_for_status()
            print(f"✅ 入库 {source}: {r.json().get('chunks')} 块")
            ok += 1
        except httpx.HTTPError as exc:
            print(f"❌ 入库失败 {source}: {exc}")
    # 检索自检
    if ok:
        r = httpx.post(f"{KB_URL}/search", json={"query": "导出接口 p95 超过 SLO 怎么处理", "limit": 3}, timeout=60)
        hits = r.json()
        print("\n检索自检（导出接口性能）：")
        for h in hits:
            print(f"  - {h['source']} (score={round(h['score'], 3)}): {h['text'][:60]}...")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
