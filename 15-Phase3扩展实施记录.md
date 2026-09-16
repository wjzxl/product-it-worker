# 15 · Phase 3 扩展实施记录（应用动作 / 流程挖掘算法 / 多产品线与模型评估）

> 三项扩展：① 扩展 SOP 自进化应用动作（Prompt/知识库）；② 标准流程挖掘算法；③ 多产品线推广 + 私有化/多模型评估。

## ① 扩展应用动作：Prompt 版本化 + 知识库自动优化 ✅（已完成）

### Prompt 注册中心（`platform/prompts/`）
- `registry.yaml`（active 版本表）+ `<key>.v<N>.md`（各 Agent Prompt 全文，初始 v1 = 既有内置 Prompt）；
- Agent 键：`coder_agent` / `tester_agent` / `ops_diagnose_agent` / `prd_agent` / `tech_design_agent`；
- agent-runtime `app/prompts.py`：读取顺序 `PROMPTS_DIR` → registry active 版本 → 文件内容；缺失回退内置默认；
  **mtime 热更新**（应用后无需重启即生效）；
- 已接线：编码 Agent（coder）、测试 Agent（tester）、运维诊断 Agent（diagnose）的 system prompt 均由注册中心提供；
- 新增 `GET /prompts`：返回 active 版本 + 内容指纹（长度/sha256 前 16 位），用于版本溯源与生效验证。

### 新应用动作（process-mining `apply.py`）
| 动作 | 语义 | 校验 |
|---|---|---|
| `prompt_version` | 写入 `<key>.v<N+1>.md` 并切换 active（Agent 热更新生效） | Prompt 键必须在注册表；内容 50~8000 字符 |
| `knowledge_ingest` | 把新规范/经验写入知识库（`source=<target>/auto-<ts>.md`） | 来源前缀正则；内容 20~20000 字符 |

### 真实验证（`scripts/apply_optimizations.py`）—— **8/8 通过**
| 检查 | 结果 |
|---|---|
| ① 初始版本 | ✅ coder_agent=v1 |
| ② Prompt 新版本写入并切换 | ✅ v1 → v2 |
| ③ **热更新生效（无需重启）** | ✅ 指纹变化：v1(len=195, sha=ad0feaf8) → v2(len=270, sha=9e33f979) |
| ④ / ⑦ GitLab 改进项登记 | ✅ issue #51、#52 |
| ⑤ 知识自动入库 | ✅ `runbooks/auto-20260914-091627.md`（1 块） |
| ⑥ 运维域检索命中新知识 | ✅ top=runbooks/auto-20260914-091627.md（命中自动入库 2 条） |

### 测试
agent-runtime 34 项（新增 prompts 5：加载/热更新/缺失回退/坏配置容错）；process-mining 25 项（新增应用动作 6：Prompt 版本切换/未知键/长度校验/入库请求/非法来源/提案可应用标记）。

### 复跑修正（③ 阶段回归时发现）
该验收脚本原本**只能跑一次**（硬断言初始版本必须是 v1、Prompt 内容不含轮次标记，导致重跑时"版本没升/指纹没变"被误判失败）。
已改为**状态无关 + 内容带轮次戳**：初始版本只要求可读，期望版本 = 当前版本 +1，Prompt 内容附本轮固化标记以保证指纹必然变化。
复跑实测 **8/8 全部通过**（v4 → v5，指纹 `9e33f979…` → `78d6514e…`，GitLab 改进项 #57/#58，知识自动入库 `runbooks/auto-20260914-100854.md`）。

## ② 标准流程挖掘算法 ✅（已完成）

此前自进化提案的"证据"只来自自研的简易路径统计。本节补齐**教科书级流程挖掘算法**（`app/algorithms.py`，8 个函数），使 SOP 改进提案建立在可复核的算法结论上。

### 事件日志提取
- `extract_event_log(dsn, sop=None)`：直读 PostgreSQL `approval_requests` + `task_timeline`，把审批/GATE/状态变更还原为 **case（task）× activity × timestamp** 标准事件日志；
- `sop` 过滤参数：**这是关键修正**——平台日志是多产品线/多 SOP 异构混合（产品域 G1/G2、测试域 G4、运维域 G5/G6），不过滤时任何单一模型都会被"别的流程"淹没，导致 fitness 失真。

### 挖掘算法
| 算法 | 实现 | 输出 |
|---|---|---|
| DFG | `dfg(log)` | 直接跟随图（节点/边/频次/起点终点） |
| Heuristic Miner | `heuristic_miner(log, threshold=0.8)` | 依赖度 `(a>b − b>a)/(a>b + b>a + 1)`，过滤噪声与短循环 |
| α-Miner | `alpha_miner(log)` | 因果/并行/选择关系 → X_w 库所 + **source/sink 库所**（修正了无源汇导致的不可 replay 问题） |

### 一致性检查（Conformance Checking）
| 算法 | 方法 | 语义 |
|---|---|---|
| `conformance_dfg` | **留一法**（leave-one-out，默认，min_support=2） | 用"其余全部轨迹"建模型，逐条检查当前轨迹；对**唯一/稀有路径零容忍**，最擅长发现异常案例 |
| `conformance_heuristic` | 边级检查 | 按依赖度阈值判断每条边是否"合规" |
| `conformance_alpha` | **Token Replay** | `fitness = ½(1 − missing/consumed) + ½(1 − remaining/produced)` |
| `threshold_sweep` | 多阈值扫描 | 0.3 / 0.5 / 0.8 下的模型与 fit 对比，辅助选参 |

### 真实验证（真实 PostgreSQL 日志，非构造数据）
日志规模：**33 cases / 178 events**；DFG：10 节点 / 16 边；Heuristic（0.8）：10 条依赖（如 `GATE_TECH_DESIGN_REVIEW:approve ⇒ approval.decided` dep=0.955）；α-Net：10 activities / 16 因果关系 / 0 并行 / 50 库所，start=[`task.created`]，end=[`approval.decided`, `status_changed`, `task.created`]。

| 一致性算法 | Fitness | 偏差案例 | 结论 |
|---|---|---|---|
| **dfg_leave_one_out（默认）** | **0.97** | 1 | ✅ 精准命中唯一的运维异常案例 `tsk_70624766dd08`（告警触发流程，独有边 `approval.requested→GATE_POSTMORTEM_REVIEW:approve`） |
| heuristic (0.8) | 0.182 | 27 | ⚠️ 阈值过滤后的模型对异构日志过严，适合"找偏差点"不适合当总评分 |
| alpha_token_replay | 0.6 | 33 | ⚠️ 50 库所的严格网，对噪声敏感 |

**方法论收获（已写入实现）**：多 SOP 混合日志上，全局单一 fitness 分数没有意义——必须先按 `sop` 过滤再建模；异常发现用留一法 DFG，模型拟合优度用 token replay，两者互补而非互相替代。

### 新增接口（process-mining）
`GET /mining/event-log`｜`/mining/dfg`｜`/mining/heuristic`｜`/mining/alpha`｜`/mining/conformance?algorithm=dfg|heuristic|alpha&threshold&sop&sweep`｜`/mining/report`。

### 测试
process-mining **41 项通过**（新增算法 16：事件日志构造/DFG/依赖度公式/阈值过滤/α 因果关系/源汇库所/留一法偏差定位/启发式边检查/token replay fitness/阈值扫描边界）。全平台回归：approval-gateway 46、gitlab-bridge 24、task-center 13、agent-runtime 34、knowledge-base 4、metrics-dashboard 3、process-mining 41 = **165 项全绿**。

## ③ 多产品线推广 + 私有化/多模型评估 ✅（已完成）

### ③-A 按租户（产品线）信任隔离

**修掉的真实缺陷**：`compute_trust()` 此前只按 Gate 过滤、不带租户——意味着**新产品线会直接继承别的产品线已达成的 L1 全自动**，且任一产品线的一次否决会把所有产品线一起打成 L3。多产品线推广下这是重大风险。

- 信任作用域改为 **`<tenant>::<GATE>`**（`trust_scope()` / `split_scope()`），历史无前缀记录归入 `default` 租户（旧数据兼容）；
- `apply_penalty(gate, tenant=…)` 只写本产品线作用域：**惩罚不跨线扩散**；
- `GET /trust`（按 `X-Tenant` 隔离）｜`GET /trust/overview`（平台管理员总览：各线 Gate 数/等级分布/自动放行占比）。

### ③-B 产品线级 SOP 覆盖配置

`platform/sop-config.d/<tenant>.yaml`（含 README），优先级 **环境变量 > 租户覆盖 > sop-config.yaml 基线 > 内置默认**；
缓存键含租户与其覆盖文件 mtime，**热更新**、各产品线独立调参互不影响。三个预设画像：`strict` / `standard` / `fast`。

### ③-C 一键接入脚本与隔离验收演练

`platform/scripts/onboard_product_line.py`：生成覆盖配置 → 隔离校验 → 写入产品线知识来源（`<tenant>/runbooks/`）→（可选）注册 GitLab webhook → 输出接入报告 + 人工待办清单；`--verify` 会**在同一次接入里跑验收演练**。

**真实演练结果**（独立演练库上的真实服务，`GATE_PR_REVIEW`，各投 12 条审批全批准）：

| 验收项 | 结果 |
|---|---|
| 新产品线未继承信任（strict 画像 `min_samples=20`） | ✅ 12 样本 → **L2**（对照线同级样本已 L1） |
| 对照产品线按平台基线（`min_samples=10`） | ✅ **L1** |
| 惩罚不扩散（本线否决 1 条后） | ✅ 本线 → **L3**，对照线仍 **L1** |

报告：`platform/reports/onboard-line-pay-20260914-094844.md`。

### ③-D 知识库多产品线隔离（含两个真实踩坑）

1. **向量维度不自洽**：`LiteLLMEmbedder` 默认假定 1536 维，而 DashScope `text-embedding-v3` 实际返回 **1024** 维 → Qdrant 全部写入 400。修复：**未设置 `EMBEDDING_DIM` 时按首次返回向量自动发现**，设置了则作为契约校验（不符明确报错）；collection 改为**首次写入时按真实维度惰性创建**。
2. **检索无产品线边界**：`/search` 新增 `source_prefix`（先多取候选再按来源前缀过滤），A 产品线 Agent 不会检索到 B 产品线 Runbook。

实测（真实 Qdrant + 真实 embedding）：`line-crm/runbooks/db.md`（相似度 0.769）在 `source_prefix=line-pay/` 查询中**被正确排除**，`line-crm/` 作用域下才返回。

### ③-E 私有化/多模型评估夹具

判分原则：**机器判分优先，不编造成本**。

| 组件 | 说明 |
|---|---|
| 金标集 `platform/eval/golden/` | **10 个任务**覆盖 5 角色：coder ×3、prd ×2、tech_design、tester ×2、ops_diagnose ×2；含 3 个"陷阱/边界"高权重任务 |
| 判分器 `app/eval.py` | `hidden_tests`（沙箱跑隐藏测试，按通过比例给分）、`mutation_tests`（**变异检验**：正确实现必须全绿、含 bug 实现必须变红，抓不到只给 0.5）、`json_schema`、`required_sections`、`regex_any`、`forbidden_regex`（否定语境感知）、`required_keywords` |
| CLI `scripts/model_eval.py` | `--models`、`--limit`、`--tag`、`--mode gateway|private`；报告落 `reports/model-eval-*.md|json` |
| 私有化接入 | `EVAL_BASE_URL` / `EVAL_API_KEY` 指向任意 OpenAI 兼容端点（vLLM / Ollama / 内网部署）即可复用同一报告 |
| 成本口径 | 取网关 `x-litellm-response-cost` 头；拿不到就标 `unmetered`，不做估算（Qwen 已补价格映射，见 ③-F） |

**真实评估结果**（`reports/model-eval-cloud-v4-metered-20260916-014327.md`，10 任务全量，成本均已计量）：

| 模型 | 通过 | 加权得分 | 延迟 P50 | tokens | 成本（真实计量） |
|---|---|---|---|---|---|
| `deepseek-chat` | 9/10 | 0.935 | **1864 ms** | 7482 | **$0.007633** |
| `qwen-plus` | 10/10 | 1.000 | 8056 ms | 7828 | **$0.008068** |
| `qwen-max` | 8/10 | 0.952 | 14625 ms | 5796 | **$0.029141** |
| `mock-gpt`（弱模型桩，验证"挡住不达标模型"） | 0/5 | 0.200 | 30 ms | 450 | $0（本地桩显式置 0） |

> 上一轮（`cloud-v3`，Qwen 尚未计价）为 deepseek 9/10、qwen-plus 10/10、qwen-max 10/10；
> **同模型同任务两轮结果不同**（qwen-max 10/10 → 8/10，temperature=0 也不保证确定性），
> 该现象已写入报告的口径声明——单轮结果用于排除明显不适用者，精细对比需多轮取均值。

**夹具自身也被真实模型校准过（重要教训）**：首轮 7 任务跑出 2 个"失败"其实都是**夹具误判**——
① deepseek-chat 严格按指令直接输出代码但**不加围栏**，抽取器只认围栏块 → 误判 0 分（已支持裸代码）；
② qwen-max 把 HTTP 方法与路径**分行加粗**书写，正则要求同行 → 误判（已允许跨行非贪婪匹配）；
③ 模型把"**不建议**忽略告警"写成禁止项，朴素子串匹配判违规（已加否定语境感知）。
校准后三模型在 7 个基础任务上全部 7/7 → **说明基础集对这些前沿云模型已饱和**，据此补充 3 个高权重陷阱任务，区分度随即出现（deepseek-chat 因 JSON 漏 `severity` 字段丢分）。

**选型结论（本平台口径）**：云模型能力已足够跑通全流程，选型差异主要在**成本与延迟**（deepseek-chat 快 3–4 倍，单轮成本最低；qwen-max 最贵且本轮波动最大）；真正的门槛在**私有化小模型**——夹具的价值正是接入前的验收闸门（`mock-gpt` 桩 0/5 演示了"不达标即挡住"）。

## ③-F 收尾修复：Qwen 成本映射 + G7 指标标注 ✅

### 1) Qwen 成本映射（原"unmetered"缺口）

**根因**：Qwen 走 `openai/` 兼容路由（`api_base` 指向 DashScope 兼容端点），LiteLLM 不认识模型名 → 响应不带 `x-litellm-response-cost` → 计费日志记 0。
**修复**：在 `litellm-config.yaml` 用 `model_info` 显式声明单价（先试过 `base_model: dashscope/qwen-*`，实测会被解析成 0，故改为显式值）：

| 模型 | 单价（USD / 1M tokens） | 来源 |
|---|---|---|
| `qwen-max` | $1.6 in / $6.4 out | [Model inference pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing)（International 区，2026-09-15 更新），与 LiteLLM 内置 `dashscope/qwen-max` 一致 |
| `qwen-plus` | $0.4 in / $1.2 out | 同上（非思考模式；>256K tokens 进更高价档，已在配置注释中说明会被低估） |
| `qwen-embed`（text-embedding-v3） | $0.07 in / 0 out | [同步接口文档](https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api)（新加坡区，2026-09-11 更新；LiteLLM 价格表无此条目） |
| `mock-gpt` / `mock-embed` | 0 / 0 | 本地桩，显式置 0 避免被当作"未知价格模型" |

**验证（真实调用 + 精确算术核对）**：
- `qwen-plus` 14 in / 1 out → 14×4e-07 + 1×1.2e-06 = **6.8e-06** ✅（响应头实测 6.799999999999999e-06）
- `qwen-max` 14 in / 1 out → 14×1.6e-06 + 1×6.4e-06 = **2.88e-05** ✅
- `qwen-embed` 6 tokens → 6×7e-08 = **4.2e-07** ✅
- 全量评估复跑（`cloud-v4-metered`）：三个模型的成本列**全部为真实计量值**，不再有 unmetered。

**历史缺口审计**（`scripts/qwen_cost_audit.py`，**只读**，价格直接读配置文件避免两处漂移）：

| 模型 | 调用 | in tok | out tok | 已记录 | 应有 | 缺口 |
|---|---|---|---|---|---|---|
| `openai/qwen-max` | 61 | 55694 | 20699 | $0.0000288 | $0.221584 | **$0.2215552** |
| `openai/qwen-plus` | 29 | 4079 | 17047 | $0.0000068 | $0.022088 | **$0.0220812** |
| `openai/text-embedding-v3` | 28 | 2996 | 0 | $0.0000007 | $0.00020972 | $0.00020902 |
| **合计** | 118 | | | $0.000036 | **$0.243882** | **$0.243845** |

**平台成本口径修正**：计费库已记录总额 $0.537233（其中 deepseek $0.537197）→ 加上历史 Qwen 缺口后 ≈ **$0.7811**。
即修复前 Qwen 占**真实花费约 31%、却占已记录花费 0%**，成本面板系统性低估。脚本不修改计费日志，缺口以可复核的估算形式给出（tokens 是实测值，单价是配置中显式声明的值）。

### 2) G7 指标标注（面板曾显示原始键名）

**问题**：`14` 文档声明 G7 已"纳入度量面板标签"，但 `metrics-dashboard/app/metrics.py` 与 `scripts/metrics_report.py` 的标签映射仍只有 G1–G6（`process-mining/app/mining.py` 有），导致 **G7 在面板上显示为 `GATE_PROCESS_IMPROVEMENT` 且"域"列为 `-`**。
**修复**：两处补 `GATE_PROCESS_IMPROVEMENT → "G7 流程改进"` 与域归属 `平台域`（它改进的是平台自身流程，不属于四域）；面板标题改为"四域 + 平台域指标"、"Gate 决策（G1–G7…）"。

**真实验证**（真实库 + 真实面板渲染）：

| Gate | 域 | 审批数 | 平均决策 |
|---|---|---|---|
| G1 需求评审 | 产品域 | 6 | 0.44s |
| G2 方案评审 | 研发域 | 24 | 2.29s |
| G3 代码审查 | 研发域 | 16 | 0.57s |
| G4 放行决策 | 测试域 | 18 | 0.91s |
| G5 变更审批 | 运维域 | 4 | 1.94s |
| G6 复盘审查 | 运维域 | 2 | 0.65s |
| **G7 流程改进** | **平台域** | **12** | **0.08s** |

新增测试 2 项：标签必须为"G7 流程改进"且域为"平台域"；七个 Gate 标签齐全（防止再漏配）+ 真实库中不得出现未映射的 `GATE_` 原始键名。

### 测试
- approval-gateway **58 项**（新增多产品线 12：新线不继承信任/惩罚不跨线/单线单 Gate 惩罚/作用域键往返/租户过滤总览/SOP 覆盖生效/覆盖热更新/缺失覆盖回退/`X-Tenant` 读写隔离 404/信任接口租户视图/总览聚合）；
- knowledge-base **17 项**（新增维度守卫 5 + 自动发现 4 + 来源前缀隔离 2 + 惰性建库 2）；
- agent-runtime **76 项**（新增评估夹具 42：金标集校验 7/判分器 13/变异检验 3/裸代码抽取 4/指标汇总 6/报告渲染 3 等）；
- metrics-dashboard **5 项**（新增 G7 标签 2）；
- 全平台回归 = **234 项全绿**（approval 58、bridge 24、task-center 13、agent-runtime 76、KB 17、dashboard 5、process-mining 41）。
