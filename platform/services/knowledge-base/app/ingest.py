"""文档分块：按段落切分，约 500 字/块，50 字重叠，保留来源与序号。"""
from __future__ import annotations

import re

_CHUNK_SIZE = 500
_OVERLAP = 50


def chunk_markdown(content: str, source: str) -> list[dict]:
    """Markdown 简易分块：按标题/空行分段后合并到目标长度。"""
    parts = re.split(r"\n\s*\n", content.strip())
    chunks: list[dict] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        candidate = f"{buf}\n{part}" if buf else part
        if len(candidate) <= _CHUNK_SIZE:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            # 超长段落按窗口滑动切分
            for i in range(0, len(part), _CHUNK_SIZE - _OVERLAP):
                piece = part[i : i + _CHUNK_SIZE]
                if len(piece) > 40:
                    chunks.append(piece)
            buf = ""
    if buf:
        chunks.append(buf)
    return [{"text": c, "source": source, "index": i} for i, c in enumerate(chunks)]
