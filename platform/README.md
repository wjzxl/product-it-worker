# platform · 产品研发智能化平台代码仓库

> 自研层只做三件事：**连接（集成胶水）、审批（人审网关）、度量**。重量级能力全部组装成熟组件。
> 设计文档见仓库根目录 `01`～`06`，本目录是 Phase 0 起实施的代码骨架。

## 目录结构

```
platform/
├── docker-compose.yml        # 本地开发环境：PG / Qdrant / n8n / LiteLLM
├── litellm-config.yaml       # LLM 网关配置（强/弱模型分层路由）
├── contracts/                # 接口合约（与语言无关，先行定义）
│   ├── events.md             #   事件总线消息契约
│   ├── approval-request.schema.json  # 审批请求 JSON Schema
│   └── sop-example.yaml      #   SOP DSL 草案示例（含 G1 Gate）
└── services/
    ├── approval-gateway/     # 审批网关（T0.4 后端已完成：状态机+三要素校验+超时策略+事件发布）
    │   └── app/              #   main/db/models/schemas/router/notifier/policies/events
    └── gitlab-bridge/        # GitLab 双向集成桥（T0.2：webhook 归一化 + API 客户端 + 事件扇出）
        └── app/              #   main/client/normalize/events
```

## 快速开始

### 1. 本地开发环境（Docker）

```bash
cd platform
docker compose up -d          # PG(5432) Qdrant(6333) n8n(5678) LiteLLM(4000)
```

组件说明：
- **PostgreSQL**：业务状态（审批、任务）；本地默认库 `platform`，用户/密码见 compose；
- **Qdrant**：向量库（Phase 0 知识库检索验证）；
- **n8n**：演示流编排（访问 http://localhost:5678，初始账号见容器日志）；
- **LiteLLM**：多模型网关（Phase 0 起所有 LLM 调用必须经它，配置见 compose 环境变量）。

### 2. 审批网关

```bash
cd services/approval-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload            # http://localhost:8000
pytest                                    # 运行测试
```

API 文档：http://localhost:8000/docs

核心端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/approvals` | 创建工作流发来的审批请求（校验"三要素"齐备） |
| GET | `/approvals/{id}` | 查看审批请求与决策历史 |
| POST | `/approvals/{id}/decision` | 人做决策：approve / reject / revise（附意见） |
| POST | `/callbacks/im/card` | IM 交互卡片回调（卡片按钮 → 决策） |
| GET | `/health` | 健康检查 |

### 3. GitLab 集成桥

```bash
cd services/gitlab-bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
WEBHOOK_SECRET=xxx GITLAB_TOKEN=yyy uvicorn app.main:app --reload --port 8001
pytest
```

webhook 注册与事件映射见 [gitlab-bridge/README.md](services/gitlab-bridge/README.md)。

### 4. 环境变量（审批网关）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./approval.db` | 可切 `postgresql+psycopg://...` |
| `FEISHU_WEBHOOK_URL` | 空 | 配置后启用飞书卡片推送；为空时用日志桩 |

## 与整体方案的关系

- 审批请求模型对齐 [03-人机协作与治理](../../03-人机协作与治理.md) §3.1；
- 任务状态机对齐 [02-核心SOP流程设计](../../02-核心SOP流程设计.md) §0；
- 事件契约见 `contracts/events.md`，是集成胶水（T0.2）的实现依据。
