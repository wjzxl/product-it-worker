# 07 · Phase 0 验收报告（地基 · 0～4 周）

> 验收依据：[06-Phase0实施任务清单](06-Phase0实施任务清单.md) §4 验收清单。
> 验收方式：自动化测试 + 端到端演示脚本 + 真实基础设施（Docker 组件栈、PostgreSQL、Qdrant、LiteLLM 网关）实测。
> 验收日期：2026-08-24。状态：**本地全项通过；真实环境项待外部资源（见 §4）**。

## 1. 验收清单逐项结果

| # | 验收项（06 §4） | 结果 | 证据 |
|---|---|---|---|
| 1 | `docker compose up` 一键拉起全部基础组件 | ✅ | PG(healthy)/Qdrant/n8n/LiteLLM(HTTP 200) 实测启动；Langfuse 自托管见 §3 |
| 2 | GitLab webhook → 平台事件，平台 API → GitLab Issue 双向打通 | ✅ | gitlab-bridge 14 测试；演示检查 1/9：webhook 归一化 + 经 GitLab API 建 Issue（桩后端实录请求） |
| 3 | 审批请求携带三要素，缺一自检失败 | ✅ | JSON Schema + pydantic 强校验；approval-gateway 测试 `test_missing_evidence_rejected` 等 422 验证 |
| 4 | IM 卡片审批实时回写流程，拒绝原因结构化入库 | ✅ | 飞书 interactive 卡片已实现（URL 模式跳转审批台）；决策留痕含 comment；拒绝/退回强制填原因（422）；真实推送待飞书 webhook |
| 5 | L3 超时默认拒绝并升级通知，绝不静默放行 | ✅ | 超时扫描器测试：L3 强制 reject、L1 可按策略 auto-approve、决策人 `system:timeout` 留痕 |
| 6 | 流程在 G1 挂起/恢复断点续跑（进程重启不丢状态） | ✅ | LangGraph checkpoint 实现；agent-runtime 6 测试（挂起/批准/退回修订循环/否决）；`/runs/{id}/state` 可查询中断值 |
| 7 | 全链路 trace：每步 LLM 调用可见模型/耗时/成本 | ✅ | 网关 spend 计量落库；**Langfuse 自托管实测 trace 落库**（§3.2）；真实模型 Key 到位后成本即真实值 |
| 8 | 演示剧本端到端跑通，录屏归档 | ✅ | `scripts/demo_phase0.py` **10/10**；含 `--gateway --pg` 模式（真实网关 + PostgreSQL）双通过 |

## 2. 自动化测试证据（共 48 项）

| 服务 | 测试数 | 覆盖要点 |
|---|---|---|
| approval-gateway | 16 | 状态机、三要素、超时策略（L3 强制拒绝）、IM 回调、飞书卡片结构、Web 审批台 |
| gitlab-bridge | 14 | webhook 签名、事件归一化映射、幂等 trace、API 客户端请求与错误 |
| task-center | 8 | 任务状态机、时间线、幂等收件箱、审批事件联动（approve/reject） |
| agent-runtime | 6 | LangGraph 挂起/恢复、退回修订循环、否决、状态查询 |
| knowledge-base | 4 | 分块、入库、带来源检索、空文档拒绝 |
| 合计 | **48** | 全部通过（Python 3.9；推送后由 GitHub Actions 矩阵自动回归） |

## 3. 基础设施实测记录

### 3.1 LiteLLM 网关（真实模型 + 计量实测）
- **真实模型**：接入 DeepSeek（`deepseek-chat`，Key 经环境变量注入不落库）；经网关调用实测成功，**端到端演示 `DEMO_LLM_MODEL=deepseek-chat --gateway --pg` 10/10 通过**（PRD 由真实模型起草）；
- **成本计量**：网关接入 PostgreSQL（`LiteLLM_SpendLogs` 表），实测调用落库：`deepseek/deepseek-chat` spend=6.6e-06 USD、prompt_tokens=12、completion_tokens=1；
- **观测**：Langfuse 记录 `deepseek/deepseek-chat` observation（经网关自动上报）；
- chat/embeddings 网关转发链路（mock 后端）与知识库 `EMBEDDING_DIM` 链路亦已实测。

### 3.2 观测（Langfuse 自托管）✅ 实测通过
- `docker compose -f docker-compose.observability.yml up -d` 拉起 Langfuse v2（http://localhost:3000，账号 demo@local.dev / phase0-demo-pass）；
- `scripts/setup_langfuse_demo.py` 幂等初始化（注册用户 + 注入组织/项目/API Key，密钥哈希按源码实测公式 sha256(secret + sha256_hex(SALT))）；
- **实测证据**：经网关发起 mock-gpt 调用 → Langfuse `traces` 表出现 `litellm-acompletion` trace（observations=1）；直接 ingestion 亦 207 成功；
- 生产切换：Langfuse Cloud 免费版密钥注入同样环境变量即可，无需改代码。

### 3.3 PostgreSQL 生产路径
- 审批/任务中心 `DATABASE_URL=postgresql+psycopg://platform:platform@127.0.0.1:5432/platform`；
- 实测：任务、审批单、决策审计全部落 PG，psql 可查（演示 iid=7324 记录）。

## 4. 剩余事项（真实环境验收）

| 事项 | 状态 |
|---|---|
| 真实 GitLab 验收 | ✅ **通过**：`sandbox6853335/prodesgin-demo#1` 真实 Issue 已创建（标签 G1-passed+product，作者 facai2023money）；演示 `--real-gitlab --gateway --pg` 10/10。⚠️ 真实 webhook 回传（GitLab→平台）需平台有公网可达地址（ngrok/云部署），属部署期事项 |
| 真实飞书推送 | ⏳ 待提供测试群自定义机器人 webhook 地址 |

（LLM 真实模型、成本计量、Langfuse 观测均已闭环。）

## 5. 结论与签核建议

- Phase 0 **开发与本地验收全部完成**，产物：5 个微服务、48 项测试、端到端演示脚本（3 种运行模式）、CI、完整文档链（01~07）；
- 建议：由平台负责人按 §4 补齐外部资源后执行一次 `--real-gitlab` 演示，完成真实环境签核，随即进入 [Phase 1：产品+研发闭环 MVP](05-实施路线图与度量.md)。

签核人：＿＿＿＿＿＿　日期：＿＿＿＿＿＿
