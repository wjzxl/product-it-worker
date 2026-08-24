# knowledge-base · 知识库 v0（T0.6）

> 文档分块 → 向量化（经 LiteLLM 网关）→ Qdrant 入库 → 带来源检索。
>
> 选型说明：设计文档原选 **RAGFlow**；实施收敛为 **Qdrant + LiteLLM embeddings** 轻量链路
> （Qdrant 已在 compose 中、链路可控可测、离线可跑），对外接口不变，RAGFlow 留作 Phase 1 升级项。

## 快速开始

```bash
cd platform/services/knowledge-base
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8004   # API 文档 http://localhost:8004/docs
pytest
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant 地址（docker compose 已含）；不可达时自动降级内存桩 |
| `KB_COLLECTION` | `platform_kb` | collection 名称 |
| `LLM_API_KEY` | 空 | 为空时用哈希向量桩（离线可跑）；配置后走 LiteLLM `/embeddings` |
| `LLM_BASE_URL` | `http://localhost:4000/v1` | LiteLLM 网关 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 嵌入模型（需在网关注册） |

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/documents` | 入库：{source, content} → 分块（~500 字/块，50 字重叠）→ 向量化 → Qdrant |
| POST | `/search` | 检索：{query, limit} → 命中列表（text + source + score） |

## 验收对应（06 文档 T0.6）

- 产品文档入库 ✓（POST /documents，PRD/方案/复盘均可）
- 检索接口带来源 ✓（每个命中含 source，来源可点击可溯源）
- 后续：Agent 运行时在"背景检索"步骤调用 /search，把命中作为 PRD 草稿的依据链接（Phase 0 已预留 evidence 占位）
