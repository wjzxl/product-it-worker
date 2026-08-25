# 06 · Phase 0 实施任务清单（地基 · 0～4 周）

> 前置确认：`01`/`02` 中的 **6 个 Gate（G1～G6）已评审通过，匹配团队决策习惯**，作为本平台人审闸门的基线配置。若评审时做了微调，请同步告知以修订 `02` 的 Gate 总览表。
>
> Phase 0 目标：打通集成与基础设施，跑通一条**演示流**：需求输入 → PRD 起草 → G1 审批（IM 卡片）→ 批准后自动建 GitLab Issue，全链路留痕可追踪。

## 1. 技术基线（Phase 0 范围）

| 项 | 选择 | 说明 |
|---|---|---|
| 自研层语言 | **Python 3.9+ / FastAPI** | 与 LangGraph、LiteLLM、RAGFlow 生态同语言，小团队维护成本最低；如团队以 TS 为主可整体替换为 Node/NestJS，合约与数据模型不变 |
| 审批网关 | 自研（本目录 `platform/services/approval-gateway`） | Phase 0 交付最小可运行版：审批状态机 + Web API + IM 卡片回调 + 超时升级 |
| 工作流引擎 | n8n（Docker） | Phase 0 只用于演示流编排，不写业务逻辑 |
| 模型接入 | LiteLLM 网关（Docker） | 多模型路由 + 成本计量 |
| 知识库 | RAGFlow + Qdrant（Docker） | 产品文档入库与检索验证 |
| 可观测 | Langfuse | 全链路上报（Phase 0 可先用 Cloud 免费版，自托管见 compose 注释） |

## 2. 任务分解

| # | 任务 | 产出物 | 依赖 | 工时 | 验收标准 |
|---|---|---|---|---|---|
| T0.1 | 仓库脚手架与本地开发环境 | `platform/docker-compose.yml`、`.gitignore`、开发 README | — | 1d | `docker compose up` 一键拉起 PG/Qdrant/n8n/LiteLLM；审批网关本地可启动 |
| T0.2 | GitLab 双向集成适配器 | webhook 接收端点 + API Client（建 Issue/读 MR/回读 CI 结果） | T0.1 | 2d | 在测试仓库 push/开 Issue 能触发平台事件；平台能程序化创建 Issue |
| T0.3 | LLM 网关接入与观测 | LiteLLM 多模型配置 + Langfuse 上报 | T0.1 | 1d | 一次 LLM 调用在 Langfuse 可见 trace/成本；模型故障时降级到备用模型 |
| T0.4 | **审批网关 v0**（核心自研件） | 审批 API + 状态机 + 超时升级 + 飞书卡片回调 + Web 审批台 v0 | T0.1 | 3d | 创建审批→IM 卡片→批准/拒绝/退回→决策回流留痕，全流程可查；L3 超时默认拒绝 |
| T0.5 | 任务中心骨架 | 任务表/时间线事件表/状态机（`02` 第 0 节） | T0.1 | 2d | 任务状态流转合法、每次流转产生时间线事件 |
| T0.6 | 知识库 v0 | RAGFlow 部署、产品文档入库、检索接口验证 | T0.1 | 2d | 给定查询能返回带来源的文档片段，来源可点击 |
| T0.7 | Agent 运行时最小验证 | LangGraph 骨架：PRD 起草 Agent + G1 处 `interrupt` 挂起/恢复 | T0.2/T0.3/T0.6 | 3d | 流程能在 Gate 处挂起，人审批后从断点恢复继续执行 |
| T0.8 | 演示流串接 | 需求输入→PRD 起草→G1 卡片审批→批准→建 Issue | T0.4/T0.5/T0.7 | 2d | 见 §3 演示剧本，端到端无人工介入（除 G1 审批） |
| T0.9 | 验收与演示 | 验收报告 + 演示录屏 | T0.8 | 1d | §4 验收清单全部通过 |

**合计约 17 人日**（1 名平台工程师，4 周含余量）。

## 3. 演示剧本（T0.8 验收用）

```
1. 人在聊天入口输入："用户想要一个导出报表的功能，支持按日期筛选"
2. 产品 Agent（LangGraph）：
   a. 澄清对话（2 轮内收敛）→ 检索知识库历史需求
   b. 起草 PRD 草案（含 EARS 验收标准 + 依据链接）
3. 流程到达 G1：审批网关创建 ApprovalRequest（三要素上下文齐备）
   → 飞书卡片推送给产品负责人
4. 产品负责人手机点击【批准】
   → 决策回流 → 流程从断点恢复
5. 产品 Agent 在 GitLab 测试仓库创建 Epic/Issue（含验收标准字段）
6. Langfuse 全链路：一条 trace 贯穿 1→5，含 LLM 成本与耗时
```

## 4. Phase 0 验收清单

- [ ] `docker compose up` 一键拉起全部基础组件
- [ ] GitLab webhook → 平台事件，平台 API → GitLab Issue 双向打通
- [ ] 审批请求携带三要素（做了什么/依据/风险自评），缺一自检失败
- [ ] IM 卡片审批（批准/拒绝/退回）实时回写流程，拒绝原因结构化入库
- [ ] L3 级审批超时默认拒绝并升级通知，绝不静默放行
- [ ] 流程在 G1 挂起/恢复断点续跑（进程重启不丢状态）
- [ ] 全链路 Langfuse trace：每步 LLM 调用可见模型/耗时/成本
- [ ] 演示剧本端到端跑通，录屏归档

## 5. Phase 0 明确不做（防蔓延）

- ❌ 不做研发域编码 Agent（Phase 1）
- ❌ 不做动态信任与风险分计算（Phase 2/3）
- ❌ 不做多租户、精细 RBAC（当前单团队）
- ❌ 不做自托管 Langfuse（先用 Cloud 免费版，compose 中留注释）

## 6. 风险与阻塞项

| 风险 | 对策 |
|---|---|
| 飞书/企微机器人审批卡片的申请与审核周期 | 先用**测试群自定义机器人 webhook**起步；卡片回调走平台回调端点 |
| 生产 GitLab 无测试环境 | 在 GitLab 建 **sandbox 测试组/仓库**，与生产权限隔离 |
| 模型 Key 管理分散 | 统一放 LiteLLM 网关环境变量，代码/仓库中不落 Key |
| RAGFlow 首次索引耗时 | 用少量高质量文档（10 篇以内）先验证链路，再批量 |

---

*本任务清单与 `platform/` 骨架代码配套。Phase 0 完成后进入 [05-实施路线图与度量](05-实施路线图与度量.md) 的 Phase 1：产品+研发闭环 MVP。*

---

## 7. 进度跟踪（随实施更新）

| # | 任务 | 状态 |
|---|---|---|
| T0.1 | 仓库脚手架与本地开发环境 | ✅ 完成（compose 已验证：PG/Qdrant/n8n/LiteLLM 全部启动健康） |
| T0.2 | GitLab 双向集成适配器 | ✅ 完成（`services/gitlab-bridge`，14 测试；webhook 归一化 + API 客户端 + 事件扇出） |
| T0.3 | LLM 网关接入与观测 | ✅ **完成**：DeepSeek `deepseek-chat` 真实模型经网关实测（演示 10/10）；Langfuse trace 落库（`deepseek/deepseek-chat`）；成本计量落 PG（`LiteLLM_SpendLogs` 实测 spend/tokens） |
| T0.4 | 审批网关 v0 | ✅ 完成（16 测试：状态机/三要素/超时扫描/L3 强制拒绝/飞书卡片/Web 审批台/事件发布）；真实飞书推送待配 `FEISHU_WEBHOOK_URL` |
| T0.5 | 任务中心骨架 | ✅ 完成（`services/task-center`，8 测试：状态机/时间线/幂等收件箱/审批联动） |
| T0.6 | 知识库 v0 | ✅ 完成（`services/knowledge-base`，4 测试 + 真实 Qdrant 冒烟通过；选型收敛为 Qdrant+LiteLLM，RAGFlow 留作 Phase 1 升级） |
| T0.7 | Agent 运行时最小验证 | ✅ 完成（`services/agent-runtime`，6 测试：LangGraph G1 挂起/恢复/退回修订/否决） |
| T0.8 | 演示流串接 | ✅ 完成（`scripts/demo_phase0.py`，端到端 10/10 通过） |
| T0.9 | 验收与演示 | ✅ **完成**：真实 GitLab（`sandbox6853335/prodesgin-demo#1/#2`）+ 真实飞书推送（G1 卡片到群，0 失败）+ 真实 DeepSeek 模型，演示 10/10；验收报告见 [07-Phase0验收报告](07-Phase0验收报告.md) |

### 外部资源状态（全部到位 ✅）

1. ~~**GitLab**~~ ✅ 真实验收通过：`sandbox6853335/prodesgin-demo#1/#2`；
2. ~~**飞书**~~ ✅ 真实卡片推送验证通过（G1 审批卡片到群）；
3. ~~**LLM**~~ ✅ DeepSeek `deepseek-chat` 已接入并实测。

**Phase 0 全部完成。**
