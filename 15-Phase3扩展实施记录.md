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
| 成本口径 | 取网关 `x-litellm-response-cost` 头；**拿不到就标 `unmetered`**（Qwen 自定义 DashScope 端点无价格映射），不做估算 |

**真实评估结果**（`reports/model-eval-cloud-v3-20260914-100524.md`，10 任务全量）：

| 模型 | 通过 | 加权得分 | 延迟 P50 | tokens | 成本 |
|---|---|---|---|---|---|
| `deepseek-chat` | 9/10 | 0.935 | **2608 ms** | 8417 | **$0.008755（已计量）** |
| `qwen-plus` | 10/10 | 1.000 | 8522 ms | 8136 | unmetered |
| `qwen-max` | 10/10 | 1.000 | 10883 ms | 5412 | unmetered |
| `mock-gpt`（弱模型桩，验证"挡住不达标模型"） | **0/5** | 0.200 | 30 ms | 450 | unmetered |

**夹具自身也被真实模型校准过（重要教训）**：首轮 7 任务跑出 2 个"失败"其实都是**夹具误判**——
① deepseek-chat 严格按指令直接输出代码但**不加围栏**，抽取器只认围栏块 → 误判 0 分（已支持裸代码）；
② qwen-max 把 HTTP 方法与路径**分行加粗**书写，正则要求同行 → 误判（已允许跨行非贪婪匹配）；
③ 模型把"**不建议**忽略告警"写成禁止项，朴素子串匹配判违规（已加否定语境感知）。
校准后三模型在 7 个基础任务上全部 7/7 → **说明基础集对这些前沿云模型已饱和**，据此补充 3 个高权重陷阱任务，区分度随即出现（deepseek-chat 因 JSON 漏 `severity` 字段丢分）。

**选型结论（本平台口径）**：云模型能力已足够跑通全流程，选型差异主要在**成本与延迟**（deepseek-chat 快 3–4 倍且成本可计量）；真正的门槛在**私有化小模型**——夹具的价值正是接入前的验收闸门（`mock-gpt` 桩 0/5 演示了"不达标即挡住"）。

### 测试
- approval-gateway **58 项**（新增多产品线 12：新线不继承信任/惩罚不跨线/单线单 Gate 惩罚/作用域键往返/租户过滤总览/SOP 覆盖生效/覆盖热更新/缺失覆盖回退/`X-Tenant` 读写隔离 404/信任接口租户视图/总览聚合）；
- knowledge-base **17 项**（新增维度守卫 5 + 自动发现 4 + 来源前缀隔离 2 + 惰性建库 2）；
- agent-runtime **76 项**（新增评估夹具 42：金标集校验 7/判分器 13/变异检验 3/裸代码抽取 4/指标汇总 6/报告渲染 3 等）；
- 全平台回归 = **232 项全绿**（approval 58、bridge 24、task-center 13、agent-runtime 76、KB 17、dashboard 3、process-mining 41）。
