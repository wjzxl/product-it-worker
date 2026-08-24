# platform · 产品研发智能化平台代码仓库

> 自研层只做三件事：**连接（集成胶水）、审批（人审网关）、度量**。重量级能力全部组装成熟组件。
> 设计文档见仓库根目录 `01`～`06`。当前进度：Phase 0 开发完成，本地端到端演示 10/10 通过。

## 目录结构

```
platform/
├── docker-compose.yml        # 本地开发环境：PG / Qdrant / n8n / LiteLLM（已验证可启动）
├── litellm-config.yaml       # LLM 网关配置（强/弱模型分层路由）
├── contracts/                # 接口合约（与语言无关）
│   ├── events.md             #   事件总线消息契约
│   ├── approval-request.schema.json  # 审批请求 JSON Schema（三要素强校验）
│   └── sop-example.yaml      #   SOP DSL 草案示例（含 G1 Gate）
├── scripts/
│   └── demo_phase0.py        # Phase 0 端到端演示（10 项检查，一键运行）
└── services/                 # 五个微服务（Python 3.9+ / FastAPI）
    ├── approval-gateway/     # 人审网关：审批状态机 + 三要素校验 + 超时扫描 + 飞书卡片 + Web 审批台
    ├── gitlab-bridge/        # GitLab 双向集成桥：webhook 归一化 + API 客户端 + 事件扇出
    ├── task-center/          # 任务中心：状态机 + 时间线 + 平台事件接入（幂等收件箱）
    ├── agent-runtime/        # Agent 运行时：LangGraph 状态图 + G1 挂起/恢复（T0.7）
    └── knowledge-base/       # 知识库 v0：分块 → 向量化 → Qdrant → 带来源检索（T0.6）
```

各服务测试：approval-gateway 16 · gitlab-bridge 14 · task-center 8 · agent-runtime 6 · knowledge-base 4，共 **48 个测试**；推送后由 [.github/workflows/ci.yml](../.github/workflows/ci.yml) 自动回归（Python 3.9 矩阵）。

## 快速开始

### 1. 本地开发环境（Docker）

```bash
cd platform
docker compose up -d          # PG(5432) Qdrant(6333) n8n(5678) LiteLLM(4000)
```

- **PostgreSQL**：业务状态（`platform/platform`）；
- **Qdrant**：知识库向量存储；
- **n8n**：http://localhost:5678（Phase 1 起承载流程编排，Phase 0 用 demo 脚本编排）；
- **LiteLLM**：http://localhost:4000，所有 LLM 调用必经网关（配置见 `litellm-config.yaml`，Key 用环境变量注入不落库）。

### 2. 启动服务（各目录均有 README）

```bash
cd services/approval-gateway && source .venv/bin/activate && uvicorn app.main:app --port 8000   # 审批网关 + Web 审批台 http://localhost:8000
# gitlab-bridge → 8001（README 有 webhook 注册说明）
# task-center   → 8002
# agent-runtime → 8003
# knowledge-base→ 8004
```

### 3. 端到端演示（Phase 0 验收，10 项检查）

```bash
cd platform
services/agent-runtime/.venv/bin/python scripts/demo_phase0.py              # 基础模式（离线桩）
python3 scripts/mock_llm_server.py &                                        # （可选）mock 模型后端
services/agent-runtime/.venv/bin/python scripts/demo_phase0.py --gateway --pg   # 真实 LiteLLM 网关 + PostgreSQL
GITLAB_URL=... GITLAB_TOKEN=... WEBHOOK_SECRET=... \
  services/agent-runtime/.venv/bin/python scripts/demo_phase0.py --gateway --pg --real-gitlab   # 接入真实 GitLab
```

自动完成：启动四服务（+GitLab 桩）→ 模拟 Issue webhook → 事件扇出建任务 → Agent 起草 PRD →
G1 挂起 → 人审批准 → 事件回流 → 恢复执行 → 建 Issue → 全链路时间线与审计输出。
`--gateway` 使 LLM 调用走 LiteLLM 网关（默认 mock-gpt，配真实 Key 后改 `DEMO_LLM_MODEL` 即可切换真实模型）。
`--pg` 使审批/任务中心落 PostgreSQL，可在 pgAdmin/psql 中直接查看审计数据。

## 服务间协作方式

```
GitLab webhook ──> gitlab-bridge ──事件扇出(PUBLISH_TARGETS)──> task-center（任务/时间线）
                                                                     ▲
agent-runtime（LangGraph）──发起审批──> approval-gateway ──事件扇出──┘
     ▲                                        │
     └──────────── 人审决策后 resume（Phase 1 起由 n8n/Temporal 自动驱动）┘
knowledge-base ──检索──> agent-runtime（PRD 依据，Phase 0 为占位）
```

## 环境变量速查

| 变量 | 服务 | 说明 |
|---|---|---|
| `DATABASE_URL` | approval/task-center | 默认 SQLite，生产切 PG |
| `PUBLISH_TARGETS` | bridge/approval | 事件扇出目标（逗号分隔，指向 task-center `/events`） |
| `WEBHOOK_SECRET` / `GITLAB_URL` / `GITLAB_TOKEN` | gitlab-bridge | webhook 校验与 API 认证 |
| `FEISHU_WEBHOOK_URL` / `APPROVAL_WEB_BASE_URL` | approval-gateway | 飞书卡片推送与审批台跳转 |
| `SWEEP_INTERVAL_SECONDS` | approval-gateway | 超时扫描周期（0=关闭；或 POST /admin/sweep） |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | agent-runtime/knowledge-base | 为空时用离线桩，配置后走 LiteLLM |
| `QDRANT_URL` / `KB_COLLECTION` | knowledge-base | Qdrant 地址与 collection |

## 与整体方案的关系

- 审批请求模型对齐 [03-人机协作与治理](../../03-人机协作与治理.md) §3.1；
- 任务状态机对齐 [02-核心SOP流程设计](../../02-核心SOP流程设计.md) §0；
- 事件契约见 `contracts/events.md`；
- 实施进度见 [06-Phase0实施任务清单](../../06-Phase0实施任务清单.md) §7。
