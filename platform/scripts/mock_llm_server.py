#!/usr/bin/env python3
"""Mock LLM 后端（OpenAI 兼容）：验证 LiteLLM 网关链路时替代真实供应商，零依赖（stdlib）。

用途：T0.3 验收前哨 —— 真实模型 Key 未就绪时，先验证
「服务 → LiteLLM 网关 → 模型后端 → 返回」整条链路与网关计量。

运行：
  python3 scripts/mock_llm_server.py          # 0.0.0.0:8098（供 docker 内网关经 host.docker.internal 访问）
  MOCK_PORT=8099 python3 scripts/mock_llm_server.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("MOCK_PORT", "8098"))


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return

        if self.path.endswith("/chat/completions"):
            model = payload.get("model", "mock")
            prompt = (payload.get("messages") or [{}])[-1].get("content", "")[:200]
            content = (
                f"# PRD 草案（mock-{model} 生成）\n\n## 需求\n{prompt}\n\n"
                "## 验收标准\n- WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据\n"
                "- WHEN 用户点击导出 THEN 系统生成 CSV 文件并开始下载\n"
            )
            self._json(200, {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 60, "total_tokens": 90},
            })
        elif self.path.endswith("/embeddings"):
            inputs = payload.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            self._json(200, {
                "object": "list",
                "model": payload.get("model", "mock-embed"),
                "data": [{"object": "embedding", "index": i, "embedding": self._vector(t)} for i, t in enumerate(inputs)],
                "usage": {"prompt_tokens": sum(len(t) for t in inputs), "total_tokens": sum(len(t) for t in inputs)},
            })
        else:
            self._json(404, {"error": f"unknown path {self.path}"})

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.md5(text.encode("utf-8")).digest()
        return [(b / 255.0) * 2 - 1 for b in digest[:8]]

    def log_message(self, *args) -> None:
        print(f"[mock-llm] {self.command} {self.path}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"mock-llm listening on 0.0.0.0:{PORT}（chat/completions, embeddings, health）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
