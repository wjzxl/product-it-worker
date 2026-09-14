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
