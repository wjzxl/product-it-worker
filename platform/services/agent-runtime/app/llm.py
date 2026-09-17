"""LLM 客户端：经 LiteLLM 网关调用（OpenAI 兼容），无 Key 时用模板桩跑通流程。

T0.3 观测说明：生产配置 LLM_API_KEY 后，所有调用统一走 LiteLLM 网关
（http://localhost:4000），在 LiteLLM 侧配置 Langfuse callback 即可获得
全链路 trace 与成本计量（见 litellm-config.yaml 与 Phase 0 文档）。
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class LLMClient:
    """OpenAI 兼容 chat completions 客户端（目标：LiteLLM 网关）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.Client(timeout=timeout, transport=transport)
        # WP7：用量累计（归档与成本度量）
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.unmetered_calls = 0

    def chat(self, messages: list[dict]) -> str:
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages},
        )
        resp.raise_for_status()
        body = resp.json()
        self._record_usage(body, resp.headers)   # WP7：运行归档需要 token 与成本
        return body["choices"][0]["message"]["content"]

    # ---------- WP7 用量记账 ----------
    def _record_usage(self, body: dict, headers) -> None:
        """累计 token 与成本。成本取网关 `x-litellm-response-cost`；取不到就标 unmetered。

        与评估夹具同一口径（见 app/eval.py）：**不估算、不编造**——
        拿不到计费头就记 unmetered，让归档如实反映"这笔钱没被计量"。
        """
        usage = body.get("usage") or {}
        self.llm_calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        cost_raw = headers.get("x-litellm-response-cost") if headers is not None else None
        if cost_raw in (None, ""):
            self.unmetered_calls += 1
            return
        try:
            self.cost_usd += float(cost_raw)
        except (TypeError, ValueError):
            self.unmetered_calls += 1

    def usage(self) -> dict:
        """本次运行（该客户端实例）的累计用量。"""
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_metered": self.unmetered_calls == 0,
        }

    def reset_usage(self) -> None:
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.unmetered_calls = 0


class TemplateLLM:
    """无 Key 时的模板桩：根据 prompt 生成占位 PRD，保证流程可端到端演示。"""

    def chat(self, messages: list[dict]) -> str:
        prompt = messages[-1]["content"]
        head = prompt.split("\n")[0][:80]
        return (
            f"# PRD 草案（TemplateLLM 生成）\n\n"
            f"## 需求\n{head}\n\n"
            f"## 验收标准\n"
            f"- WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据\n"
            f"- WHEN 用户点击导出 THEN 系统生成 CSV 文件并开始下载\n"
        )


class MockLLM:
    """测试用：按队列返回预置答案。"""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)

    def chat(self, messages: list[dict]) -> str:
        return self.answers.pop(0)


def llm_from_env() -> Any:
    key = os.getenv("LLM_API_KEY", "")
    if key:
        return LLMClient(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:4000/v1"),
            api_key=key,
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        )
    return TemplateLLM()
