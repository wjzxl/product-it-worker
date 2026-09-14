# 14 · Phase 3 流程挖掘与 SOP 自进化实施记录

> 目标（05 Phase 3）：**从平台自身数据挖掘瓶颈 → 提案优化 SOP → 人审 → 应用 → 度量**，用平台自身流程改进平台。

## 1. 交付内容

### 流程挖掘服务（`services/process-mining`）
| 能力 | 说明 |
|---|---|
| **Gate 瓶颈** | 每 Gate 的请求/决策数、**决策等待 p50/p95**、**超时率**（system:timeout）、**退回率**（revise+reject） |
| **退回原因** | 从真实决策意见（comment）提取 Top 退回原因，供提案引用 |
| **流程变体** | 从 `task_events` 还原每个任务的事件序列（variant）并聚类，统计出现次数与平均时长，识别**缺审批留痕的偏离路径** |
| **成本热点** | 按模型的调用数/花费/tokens（LiteLLM 计量库） |
| **规则化瓶颈识别** | `high_rework` / `slow_gate` / `timeout` / `variant_deviation` 四类发现（作为 LLM 提案的客观输入） |

### SOP 优化提案（LLM + 兜底）
- LLM（Qwen，经 LiteLLM 网关）输出**结构化提案**：`{title, rationale, evidence, action:{type,target,value}, risk}`；
- 动作类型：`trust_threshold`（信任参数）/ `gate_sla`（Gate SLA）/ `advice`（建议类，不改配置）；
- **提案校正**：LLM 若把流程变体当成 `gate_sla` 目标 → 自动降级为 `advice` 并标记 `adjusted`；
- LLM 不可用 → 规则化提案兜底（保证自进化链路始终可用）。

### G7 流程改进人审闸门
- 新增 Gate 类型 **`GATE_PROCESS_IMPROVEMENT`**（平台域 G7），纳入审批网关（三要素：提案/挖掘依据/风险）、信任分级与度量面板标签；
- 提案必须先经 G7 批准才能应用——**配置变更同样受人审约束**。

### SOP 配置中心 + 自动应用
- `platform/sop-config.yaml`：信任参数（窗口/阈值/抽样比例/惩罚时长）与各 Gate SLA；
- 服务读取顺序：**环境变量 > 配置文件 > 内置默认**；配置支持 **mtime 热更新**（应用后无需重启即生效）；
- 应用校验（演练中发现的真实缺陷驱动）：`gate_sla` 目标必须形如 `GATE_*`、`trust_threshold` 参数必须在白名单且取值在合理区间，否则**拒绝应用**；
- 应用同时**登记 GitLab 改进项**（标签 `process-improvement`），形成可追踪的改进台账。

## 2. 真实端到端验证（`scripts/phase3_evolve.py --gateway 环境`）——**9/9 通过**

### ① 挖掘（真实 PostgreSQL 数据）
```
G2 方案评审  请求=24 决策=24 等待p50=1.75s p95=5.55s 退回=12%
G1 需求评审  请求= 6 决策= 6 等待p50=0.11s p95=1.70s 退回=33%
G3 代码审查  请求=16 决策=16 等待p50=0.64s p95=1.36s 退回= 0%
G4 放行决策  请求=18 决策=18 等待p50=0.75s p95=1.18s 退回= 0%
成本：deepseek-chat 174 次 / qwen-max 33 次 / text-embedding-v3 6 次
```
瓶颈发现：`high_rework`（G1 退回/否决率 33%）、`variant_deviation`（产品域任务缺完整审批留痕）。

### ② 提案（Qwen）
- `[prop-1] 优化需求评审流程以减少返工`（advice，依据 G1 33%）
- `[prop-2] 加强多租户验证清理相关培训`（advice，依据退回原因聚类）

### ③④⑤⑥⑦ 应用与度量
| 步骤 | 结果 |
|---|---|
| ③ 进入 G7 闸门 | ✅ `GATE_PROCESS_IMPROVEMENT` 待审（approval `apv_1a6ee984c219`） |
| ④ 人审批准 → 应用 | ✅ `sample_ratio: 0.2 → 0.3`（数据驱动兜底提案：高返工 Gate 提高抽样覆盖） |
| ⑤ GitLab 改进项 | ✅ **issue #48** 已登记（可追踪效果） |
| ⑥ 配置版本 | ✅ version=4（每次应用递增，可追溯） |
| ⑦ 热更新生效 | ✅ 无需重启，L2 Gate 抽样即时变为 0.3 |

## 3. 演练中发现并修复的真实缺陷（自进化的价值闭环）

| 缺陷 | 现象 | 修复 |
|---|---|---|
| LLM 提案目标非法 | Qwen 把**流程变体字符串**当作 `gate_sla` 的 target → 被机械写入配置，污染 `sop-config.yaml` | ① 应用层校验（Gate 名正则 + 参数白名单/区间）；② 提案校正（非法目标自动降级 `advice`）；③ prompt 强化；④ 清理被污染配置项 |
| 配置缓存不感知外部变更 | 提案应用后需重启才生效 | 配置读取改为 **mtime 热更新** |

## 4. 测试证据（全平台 **138 项**全绿）

approval-gateway 46 ｜ gitlab-bridge 24 ｜ task-center 13 ｜ agent-runtime 29 ｜ knowledge-base 4 ｜ metrics-dashboard 3 ｜ **process-mining 19**（分位数/瓶颈识别/提案解析与降级/LLM 失败回退/配置应用与校验/审批门禁/热更新/真实 PG 集成）。

## 5. 与设计对照

| 设计（05 Phase 3） | 结果 |
|---|---|
| 流程挖掘：从时间线找瓶颈（Gate 等待时长、返工热点） | ✅ 完成（等待 p50/p95、超时率、退回率、变体、成本） |
| 提案优化 SOP——用平台自身流程改进平台 | ✅ 完成（LLM 结构化提案 + G7 人审 + 自动应用 + 改进项台账） |
| 度量验证 | ✅ 配置版本/生效抽样比例可观测；改进项在 GitLab 可追踪 |
| 动态信任（Phase 3 另一项） | ✅ 已完成（见 [13 文档](13-生产化与Phase3实施记录.md) ②） |
| 多产品线推广 / 私有化模型评估 / 成本精调 | ⏳ 后续（多租户已就绪，模型网关可多供应商热插拔） |

## 6. 已知限制

| 限制 | 影响 | 后续 |
|---|---|---|
| 提案质量依赖模型（Qwen 倾向产出建议类） | 可自动应用的提案偏少 | 已加数据驱动兜底提案；可换强模型或引入提案模板库 |
| 变体分析基于事件序列字符串 | 复杂并行流程还原能力有限 | 可引入标准流程挖掘算法（α-miner/启发式） |
| 应用仅覆盖信任参数与 SLA | 尚未覆盖 Prompt/知识库/SOP 结构 | 扩展动作类型（prompt_version/knowledge_ingest/gate_topology） |
