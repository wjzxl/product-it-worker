# 17 · FDE 范式落地开发计划

> 上游依据：[16-FDE交付范式对标与目标架构](16-FDE交付范式对标与目标架构.md)（缺口 D1–D8、设计决定 D1–D7、工作包 W1–W11）。
> 本计划把 W1–W11 展开为**可排期、可判定、可回滚**的 13 个工作包，并给出契约定义、测试策略、灰度与 kill criteria。

---

## 0.0 执行状态（收尾更新）

| 工作包 | 状态 | 验收证据 |
|---|---|---|
| WP0 工程基线 | ✅ 完成 | CI 7 服务矩阵 + `gates` job（规则自检 + 规则包校验） |
| WP1 需求来源链 | ✅ 完成 | 演练 12/12（`reports/wp1-provenance-drill-*.md`） |
| WP2 工程闸门 E1 | ✅ 完成 | 演练 10/10（`reports/wp2-eng-gate-e1-drill-*.md`） |
| WP3 工程闸门 E2/E3 | ✅ 完成 | 演练 10/10（`reports/wp3-eng-gates-e2-e3-drill-*.md`） |
| WP4 项目规则包 | ✅ 完成 | 三层规则 + 五份模板 + 校验器（12 项反例测试）+ prompt 实际引用 |
| WP5 证据库 | ✅ 完成 | 演练 7/7（含独立复验 3/3） |
| M1 决策点 | ✅ 完成 | [18-M1复盘与P1决策](18-M1复盘与P1决策.md) |
| WP6 岗位注册表 | ✅ 完成 | 3 个岗位三件套 + `/roles`；边界进入 system prompt |
| WP7 持久化运行时 | ✅ 完成 | 演练 10/10（kill -9 恢复 + 等待/唤醒 + 归档 + cron 触发器） |
| WP8 工具注册表 | ✅ 完成 | 演练 8/8（两种边界 + 审计 + 不改代码加工具） |
| WP9 决策分发 | ✅ 完成 | 演练 6/6（真实写 KB 并检索命中） |
| WP10 驾驶舱 | ✅ 完成 | 演练 8/8（含误拦反馈闭环，误拦率可计算） |
| M2 决策点 | ✅ 完成（部分项未验证） | [19-M2复盘与P1验收](19-M2复盘与P1验收.md) |
| WP11 多模态 / WP12 复用度量 | ⏸️ P2，按需 | 依赖飞书权限与合规结论 |

**测试**：234（基线）→ **401**。**演练脚本 9 个、报告 9 份。**
**未验证项**（需真实运营，不写成通过）：生产拦截率/误拦率、岗位连续运行 ≥3 天、真实成本分布。

## 0. 一页读完

| 项 | 内容 |
|---|---|
| **目标** | 用 FDE 范式补齐平台 4 类缺口：① 需求来源链与澄清机制；② 会硬拦的工程闸门（E1–E3）；③ 项目级规则与设计资产；④ 岗位常驻运行 + 工具权限 + 决策台 |
| **周期** | P0 约 5–7 周，P1 约 6–8 周，P2 视需要（按 1 名熟悉本仓库的主力工程师估；第 2 人并行可压到约 8–10 周总量，但需注意契约冻结期串行） |
| **总量** | P0 26–36 人日，P1 27–39 人日，P2 13–20 人日（均为**估算**，里程碑 M1 后重估一次） |
| **架构选型** | **内聚演进**：P0 **不新增任何服务**（全部落在 approval-gateway / task-center / agent-runtime 内）；P1 最多新增 **1 个后台进程**（scheduler），不新增微服务 |
| **第一刀** | WP1 来源链 + WP2 工程闸门 E1，跑在平台自身的研发流水线上（不依赖任何外部审批） |
| **不做** | 不复制课程的文件数量；不做 Computer Use；不做跨客户资产市场；不改现有 G1–G7 语义 |
| **停止条件** | 见 §9：规则维护成本高于节省、闸门误拦率超标、高频异常覆盖不住 → 收窄或停止 |

---

## 1. 交付物地图（仓库级）

| 类型 | 路径 | 归属 | 阶段 |
|---|---|---|---|
| 改造 | `platform/services/approval-gateway/app/eng_gates.py`（新增模块） | E 闸门判定核心 + API + 留痕 | P0 |
| 改造 | `platform/services/approval-gateway/app/gate_rules.py`（新增，纯函数库） | 规则加载与判定（可单测/CLI 复用） | P0 |
| 新增资产 | `platform/engineering-gates/*.yaml`（E1/E2/E3 规则） | 规则即代码 | P0 |
| 改造 | `platform/services/approval-gateway/app/schemas.py` | `ContextSnapshot.sources[]`、闸门结果模型 | P0 |
| 改造 | `platform/services/agent-runtime/app/coder.py`、`app/apis.py` 调用点 | 生成前强制 E1 | P0 |
| 改造 | `platform/services/agent-runtime/app/apis.py` | 新增 `GateAPI` 客户端 | P0 |
| 新增资产 | `platform/project-rules/{constraints,design,completeness}/` + `manifest.yaml` | 三层规则库 | P0 |
| 新增资产 | `platform/project-rules/templates/`（五份设计骨架） | 设计模板 | P0 |
| 新增脚本 | `platform/scripts/check_project_rules.py` | 规则包校验（进 CI） | P0 |
| 新增脚本 | `platform/scripts/check_gates.py` | 闸门 CLI（本地/ CI 干跑） | P0 |
| 改造 | `platform/services/approval-gateway/app/{models,router}.py` | `evidence_artifacts` 表 + 上传/下载/绑定 | P0 |
| 新增资产 | `platform/evidence/`（工件卷，`.gitignore`） | 证据工件存储 | P0 |
| 改造 | `platform/services/agent-runtime/app/roles.py`（新增）+ `platform/roles/<role>/` | 岗位三件套 | P1 |
| 改造 | `platform/services/agent-runtime/app/sop.py` | MemorySaver → PG checkpointer | P1 |
| 新增进程 | `platform/services/agent-runtime/app/scheduler.py` + compose 条目 | 定时/队列触发 | P1 |
| 改造 | `platform/services/agent-runtime/app/tools.py`（新增）+ `platform/tools/*.yaml` | 工具注册表 + 执行期鉴权 | P1 |
| 改造 | `platform/services/process-mining/app/apply.py` | 新动作 `decision_broadcast` | P1 |
| 改造 | `platform/services/metrics-dashboard/app/{main,metrics}.py` | 驾驶舱：待决策队列 + 深链 + 决策提交 | P1 |
| 改造 | `platform/services/agent-runtime/app/intake.py`（新增）+ `platform/services/knowledge-base` | 多模态归集（语音/截图/Excel） | P2 |
| 改造 | `.github/workflows/ci.yml` | **补 matrix 缺失的 2 个服务** + 规则包校验 + 闸门干跑 | P0 |

---

## 2. 架构选型：为什么这样切（避免服务膨胀）

平台已有 7 个服务。课程缺口的落点若都做成微服务，会变成 12+ 个进程——对小团队是净负担。原则：**先内聚，后拆分；拆分的判据是"是否出现独立的扩缩容/权限边界"**。

| 新能力 | 落点 | 理由 | 何时该拆成独立服务 |
|---|---|---|---|
| 工程闸门 E1–E3 | **approval-gateway 内模块** | agent-runtime 已有 `APPROVAL_GATEWAY_URL` 客户端（零新接线）；复用其 DB/租户/SSO/事件；"所有闸门在一处"便于统一审计 | 当 E 闸门需要独立扩缩容，或规则判定要跑在 CI/独立权限域时 |
| 来源链 provenance | **approval-gateway Schema + agent-runtime 澄清节点** | 依据本就是审批三要素的一部分，字段扩展即可 | 不需要拆 |
| 项目规则包 | **静态资产 + 校验脚本** | 规则是文件，不是服务；读取方是 agent-runtime（已有 prompt 加载机制） | 需要多项目租户级隔离与在线编辑时 |
| 证据库 | **PG 表 + 本地卷（P0）→ 独立服务（P1 视需要）** | P0 追求最快可验证；工件量小 | 工件 >10GB 或多副本部署时 |
| 岗位注册表 | **agent-runtime 内模块 + `platform/roles/` 资产** | 岗位即 Prompt 的升级形态，同一 owner | 岗位 >20 个或需要非工程人员自助配置时 |
| 常驻运行时 | **agent-runtime + 1 个 scheduler 进程** | 复用 LangGraph 与 interrupt；scheduler 只是触发器 | 需要独立队列与水平扩缩容时 |
| 工具注册表 | **agent-runtime 内模块 + `platform/tools/*.yaml`** | 现有外部调用已集中在 bridge/sandbox/executors 三处，包装即可 | 需要第三方自助注册工具时 |
| 决策分发 | **process-mining 的 apply 动作** | 已有"提案→应用"通路与 G7 留痕，扩展动作类型即可 | 不需要拆 |

**新增依赖方向（不引入环）**：
```
agent-runtime ──► approval-gateway（E 闸门判定 + 证据登记）   ← 已有 client，新增 2 个端点
agent-runtime ──► process-mining（决策分发）
metrics-dashboard ──► approval-gateway / task-center（只读 + 决策提交，走既有接口）
scheduler（进程）──► agent-runtime
```

---

## 3. 迭代排期（2 周一个迭代）

| 迭代 | 工作包 | 出口标准（DoD） |
|---|---|---|
| **S0**（2–3 天） | WP0 | CI 补齐 7 服务矩阵；`sop.py` 注释与实现一致；基线 234 项测试全绿记录在案 |
| **S1** | WP1 | `sources[]` 落库；一条真实需求能从产物反查到原始材料出处；旧审批（无 sources）不报错 |
| **S2** | WP2 | **E1 真拦过一次**：影响范围未答时 agent-runtime 拒绝生成代码，留痕可查 |
| **S3** | WP3 + WP4 | E2/E3 上线（`warn` 模式）；规则包校验进 CI；五份设计模板可被设计 Agent 消费 |
| **S4** | WP5 | 验收项↔证据绑定；随机 3 项由未参与者仅凭材料复验通过 |
| **M1 决策点** | — | 复盘 P0：估时重估、误拦率、规则维护成本；决定是否进 P1 |
| **S5** | WP6 + WP7 | 一个岗位（运维值班）连续运行 ≥3 天：等待/恢复/归档可查 |
| **S6** | WP8 | 越权调用被**工具层拒绝**并留日志；新增一个工具不改 agent 代码 |
| **S7** | WP9 + WP10 | 一次决定分发到 3 个岗位规则；驾驶舱可点开待决策并提交 |
| **M2 决策点** | — | 岗位运行成本与人工介入分布复盘；决定是否进 P2 |
| **S8+** | WP11 / WP12 | 依外部条件（飞书多模态权限）与业务收益决定 |

---

## 4. 工作包详述

### WP0 · 工程基线与债务（1 人日）
**目标**：让后续所有验证可信。
**改动点**
- `.github/workflows/ci.yml`：matrix 补 `metrics-dashboard`、`process-mining`（**当前 46 项测试从未在 CI 跑过**）；新增 step：`python scripts/check_project_rules.py`、`python scripts/check_gates.py --dry-run`。
- `services/agent-runtime/app/sop.py`：首行注释"进程重启不丢状态（checkpoint）"与实现（四处 `MemorySaver()`）不符 → 改为"Phase 0 内存态，重启丢状态；持久化见 WP7"。
**验收**：CI 7 服务全绿；注释与实现一致（评审可核对 grep 结果）。

### WP1 · 需求来源链 provenance（5–7 人日）⭐
**目标**：每条需求结论可反查到原始材料位置。
**改动点**
- `approval-gateway/app/schemas.py`：`ContextSnapshot` 增 `sources: list[SourceRef] = []`（**可选、默认空 → 存量数据与 234 项测试零破坏**）；`SourceRef{kind, ref, locator, quote, captured_at}`。
- `approval-gateway/app/router.py`：`POST /approvals` 接受并落库 `sources`（JSON 列，PG 轻量迁移同 `_ensure_tenant_column` 模式）。
- `agent-runtime/app/sop.py`：产品域澄清节点输出"待确认问题 + 每条问题的来源引用"；`app/prompts.py` 注册 `clarify_agent`。
- `agent-runtime/app/main.py`：`GET /requirements/{task_ref}/provenance` 反查接口（产物 → 来源链）。
- 模糊度检测：`app/ambiguity.py`（规则词表 + LLM 复核）：命中"常用格式/主要字段/等/常规/大数据量"→ 生成待确认项。
- "明示默认值"模板：空值策略、时区、精度、超限阈值 → 生成确认卡（复用飞书卡片通道）。
**契约（示例）**
```json
{"kind": "voice", "ref": "feishu://chat/oc_xxx/msg/om_yyy",
 "locator": {"start_ms": 41200, "end_ms": 47000},
 "quote": "我们对账是按月分仓看的，不是按天。", "captured_at": "2026-09-12T20:31:00+08:00"}
```
**验收（可判定 + 证据）**
1. 单测：`sources` 缺省兼容、落库/反查、模糊词命中率（给定 12 条样例，≥10 条命中）；
2. 真实演练：用一条含"按月分仓"口径的需求，展示**从 PRD 结论 → 语音片段时间戳**的反查链路（截图 + 接口响应存证）；
3. 反向验证：旧审批记录（无 sources）读取正常。
**风险**：多模态定位（音频时间戳）在 P0 只支持**人工粘贴出处**，P2 才自动抽取——避免阻塞。

### WP2 · 工程闸门 E1「动手前」（4–6 人日）⭐
**目标**：条件不齐时**阻止生成代码**（不是提示）。
**改动点**
- `approval-gateway/app/gate_rules.py`：规则加载（`engineering-gates/*.yaml`，mtime 热更新，沿用 `sop_config` 模式）+ 判定器（纯函数，返回缺口清单）。
- `approval-gateway/app/eng_gates.py` + `router.py`：`POST /eng-gates/{code}/check`（入参：任务/需求引用 + 上下文；出参：`{passed, gaps[], rule_version, checked_at}`）、`GET /eng-gates/checks?task_ref=`；结果落 `gate_checks` 表（租户隔离）。
- `agent-runtime/app/apis.py`：`GateAPI` 客户端；`coder.py` 在 `implement()` **进入 LLM 之前**调用 E1，`passed=false` 直接返回受阻原因，不产生分支/不调模型。
- `engineering-gates/E1-pre-implementation.yaml`：检查项 = 需求依据、引用规则、改动对象、影响范围（含"影响哪些已有接口"）、回滚方式；`mode: block|warn`。
**规则示例**
```yaml
code: E1
title: 动手之前
mode: block
checks:
  - id: requirement_basis
    ask: 本次需求的已确认依据（PRD 链接 + 来源条目）
    require: non_empty
  - id: impact_scope
    ask: 会影响哪些已有接口/表/任务
    require: non_empty
    severity: critical        # 关键未知 → 即使其它项通过也停止
  - id: rollback
    ask: 回滚方式
    require: non_empty
```
**验收**：① 单测覆盖"4/5 通过但 critical 缺失 → 仍拒绝"；② **真实拦截 ≥1 次**（留痕含缺口清单）；③ `ENG_GATES_MODE=warn` 时只记录不阻断（灰度用）；④ 现有 234 项测试不受影响。

### WP3 · 工程闸门 E2/E3（5–7 人日）
**目标**：设计完整性（E2）与交付证据（E3）可判定。
**改动点**
- `engineering-gates/E2-decomposition.yaml`：五份设计齐备 + 空态/报错态/加载态定义 + **"不适用"显式声明**（`applicable: false` 必须带 reason）。
- `engineering-gates/E3-pre-delivery.yaml`：产物清单齐 + **每个验收项有对应证据**（查 WP5 证据表）+ 方案一致性 + 接手材料（README/运行说明）。
- `agent-runtime`：设计节点后调 E2、测试/放行前调 E3；`task-center` 状态机在 `designed`/`ready_for_release` 迁移前调用（双保险）。
**验收**：① E2 用"数据/业务/接口已写但前端未定义"的样例**打回**（对应课程图 41）；② E3 用"产物 6/6、证据 5/6"样例**拒绝交付**（对应课程图 42）；③ 拦截/通过均有留痕。

### WP4 · 项目规则包与设计骨架（6–8 人日）
**目标**：把课程三层规则库落成**可校验的资产**（不追求数量）。
**改动点**
- `platform/project-rules/{constraints,design,completeness}/*.md` + `manifest.yaml`（声明适用模块与不适用项）。
- `templates/`：`data.md / business.md / api.md / frontend.md / test.md` 五份骨架（含"无界面 → 写不适用及原因"）。
- `scripts/check_project_rules.py`：校验 manifest 与文件一致、必备小节存在、不适用项有 reason、引用路径有效；进 CI。
- `agent-runtime`：设计/编码 Agent 的 prompt 注入规则包摘要（走现有 prompt registry，路径由 `PROJECT_RULES_DIR` 配置）。
**验收**：① 校验器能拦住"删了 frontend.md 却没声明不适用"；② 新建一条需求，设计产物落到五份骨架且 E2 通过；③ 规则包被 Agent 实际引用（prompt 指纹可查）。
**风险**：规则维护成本 → 用 §8 指标度量"规则维护工时 / 节省工时"。

### WP5 · 证据库与验收绑定（5–7 人日）
**目标**：验收项 ↔ 可复验工件。
**改动点**
- PG 表 `evidence_artifacts(id, tenant, task_ref, acceptance_item, kind, path, sha256, size, created_by, created_at)`；轻量迁移同既有模式。
- `approval-gateway`：`POST /evidence`（multipart 上传，限类型与大小）、`GET /evidence/{id}`、`GET /evidence?task_ref=`；工件落 `platform/evidence/<tenant>/<task_ref>/`（`.gitignore`）。
- 审批三要素的 `evidence` 字段兼容：旧字符串数组继续可用，新流程写 `evidence_ids`。
- `E3` 校验器接证据表：验收项无证据 → 缺口。
**验收**：① 上传/下载/hash 校验单测；② **复验演练**：随机 3 个已交付项，由未参与者仅凭材料复验，结论一致（记录在 `reports/`）；③ 大文件与非法类型被拒绝。

### WP6 · 岗位注册表 role-registry（3–4 人日）
**改动点**：`platform/roles/<role>/agent.md`（front-matter：职责/边界/升级路径）+ `skills.yaml`（工具 id 列表）+ `kb.yaml`（KB 来源前缀与权威级别）；`agent-runtime/app/roles.py` 加载与校验（缺项/未知工具报错）；`GET /roles`。
**验收**：① 未知工具 id 被校验拦下；② 岗位 KB 作用域生效（复用 KB `source_prefix` 隔离，已有实现）。

### WP7 · 持久化运行时（8–12 人日）🔧
**目标**：能等、能恢复、能被触发。
**改动点**：`sop.py` 换 `PostgresSaver`（依赖 `langgraph-checkpoint-postgres`，需在 pyproject 固定版本）；新增 `app/scheduler.py`（APScheduler：cron + 队列消费）+ compose 条目；等待语义：数据未就绪 → 状态 `waiting` + 唤醒条件（时间/事件）；运行归档表 `agent_runs`（输入/输出/token/成本/耗时）。
**验收**：① 进程 kill -9 后重启，任务从 checkpoint 继续（真实验证）；② "7:30 触发 → 数据未到 → 8:10 到齐 → 继续"演练通过；③ 运行归档含成本（接现有计量）。
**风险**：LangGraph 0.2.74 与 PG saver 版本兼容性 → 先在独立 venv 验证，必要时锁版本。

### WP8 · 工具注册表与执行期鉴权（8–12 人日）
**改动点**：`platform/tools/*.yaml`（id/描述/入参出参 Schema/所需权限/幂等/审计级别/执行环境）；`app/tools.py`（注册表 + 调用前鉴权 + 审计日志）；把现有 gitlab-bridge、sandbox、KB、executors 包装为注册工具；`MCP` 适配器（P1 只做 client，不做 server）。
**验收**：① 越权调用被拒并留审计日志（对应课程"两种边界"）；② 新增一个工具**不改 agent 代码**；③ 工具 Schema 校验失败时给出明确报错。

### WP9 · 决策分发（3–4 人日）
**改动点**：`process-mining/app/apply.py` 新动作 `decision_broadcast`：把已确认决定写入 KB（`authoritative: true` + 来源）并生成各岗位动作规则条目（复用 G7 审批留痕）。
**验收**：一次"清货决定"分发到 3 个岗位，各自读到同一权威来源（可查 KB 条目 id）。

### WP10 · 驾驶舱（5–7 人日）
**改动点**：`metrics-dashboard`：待决策队列（聚合 approval-gateway 待审 + E 闸门受阻 + 岗位运行等待）、任务进度/阻塞、深链到审批台/任务详情、决策提交（转调既有决策接口）；只读指标保留。
**验收**：① 点开一条待决策能看到依据与选项并提交（真实验证）；② 无审计绕过（决策仍走 approval-gateway 留痕）。

### WP11 · 多模态归集（8–12 人日 + 外部审批）P2
飞书事件订阅（语音/图片/文件）→ ASR / OCR / Excel 解析 → 候选需求 + 出处。**前置**：飞书应用权限、ASR 供应商与数据合规结论。**P0/P1 用人工粘贴出处替代**。

### WP12 · 跨项目复用度量 / Computer Use（5–8 人日）P2
复用率实测（不预设 80%）；Computer Use 仅在沙箱内、只读优先、全程录屏审计。

---

## 5. 契约与数据模型（落地即用）

**通用约束**：Python 3.9（禁用 `X | None`）、pydantic v2、新增字段一律**可选 + 默认值**、所有新表带 `tenant`、所有新接口兼容无租户头（默认 `default`）。

| 契约 | 位置 | 关键字段 |
|---|---|---|
| `SourceRef` | `contracts/approval-request.schema.json` | `kind/ref/locator/quote/captured_at` |
| 工程闸门规则 | `contracts/engineering-gate.schema.json` | `code/mode/checks[].{id,ask,require,severity}` |
| 闸门检查结果 | `contracts/gate-check.schema.json` | `passed/gaps[]/rule_version/checked_at/mode` |
| 规则包 manifest | `project-rules/manifest.yaml` | `modules[].{name,files,not_applicable[]}` |
| 岗位 manifest | `roles/<role>/agent.md` front-matter | `role/scope/tools/kb_prefix/escalation` |
| 工具注册 | `tools/<id>.yaml` | `id/params_schema/result_schema/permission/idempotent/audit` |
| 证据表 | PG DDL | `evidence_artifacts(acceptance_item, sha256, path, …)` |

---

## 6. 测试与验证策略

| 层 | 内容 | 门禁 |
|---|---|---|
| 单测 | 规则加载/判定边界、Schema 兼容、鉴权矩阵、证据 hash | 每 WP 新增测试；**基线 234 项不得回退** |
| 集成 | agent-runtime ⇄ approval-gateway（E 闸门）、task-center ⇄ 闸门、驾驶舱 ⇄ 决策 | CI 7 服务矩阵 |
| 演练（真实） | E1 拦截、E2 打回、E3 拒交付、来源反查、证据复验、断点恢复 | 每次演练写 `platform/reports/` 留证 |
| 回归 | 存量审批/任务/挖掘/评估全链路 | 234 + 新增，全绿才合并 |

**测试目标（P0 结束）**：测试总数 234 → **≥280**；新增单测集中在闸门与规则包（易回归、价值高）。

---

## 7. 兼容与灰度

- `ENG_GATES_MODE=off|warn|block`（默认 **warn**，M1 后对平台自身流水线切 `block`）；
- 按租户灰度：`X-Tenant` 既有隔离直接复用；新产品线先 warn；
- 存量数据：`sources`/`evidence_ids` 缺省兼容，迁移只加列不删列；
- 回滚：E 闸门为**旁路调用**，置 `off` 即恢复旧行为；规则文件回退即恢复旧判定；WP7 checkpointer 变更保留 `CHECKPOINTER=memory|postgres` 开关。

---

## 8. 计划自身的度量（可判定，不承诺收益）

| 指标 | 定义 | 采集 | 目标区间（M1 复盘时校准） |
|---|---|---|---|
| 闸门拦截率 | 被拦任务 / 触发检查任务 | `gate_checks` 表 | 观察值，无预设 |
| **误拦率** | 人判断为"其实可放行"的拦截 / 总拦截 | 拦截时的反馈字段 | < 10%，否则调规则 |
| 缺口补齐时长 | 从被拦到补齐通过的中位时长 | `gate_checks` 时间戳 | 观察值 |
| 规则维护成本 | 规则文件改动工时 / 月 | 人工记录 + git 统计 | **必须低于其节省的返工工时**（kill 条件） |
| 人工介入次数 | 每条岗位任务的介入次数与原因分布 | `agent_runs` | 观察值（不设"一天两次"这种硬指标） |
| 需求返工率 | 因口径/权限错误导致的返工 / 总任务 | 时间线事件 | 目标：下降 |

---

## 9. 风险登记册与 Kill Criteria

| # | 风险 | 影响 | 缓解 | 停止/收窄条件 |
|---|---|---|---|---|
| R1 | 规则维护成本高于收益 | 计划失去意义 | 规则即代码 + 校验器 + 只写"能判定"的规则 | §8 指标连续 2 个月为负 → 缩到 3 条核心规则 |
| R2 | 闸门误拦导致效率下降 | 团队绕过闸门 | warn 模式先跑 2 周；误拦率 <10%；可一键降级 | 误拦率 >20% → 回到 warn |
| R3 | LangGraph 持久化不兼容 | WP7 返工 | 先独立 venv 验证版本矩阵 | 2 天内不可解 → 自研轻量状态机（PG 表 + 轮询） |
| R4 | 岗位常驻导致成本失控 | 预算超支 | 复用预算熔断 + 每岗位预算上限 + 运行归档成本可视 | 单岗位月成本超基准 3 倍 → 降频或停 |
| R5 | 多模态数据合规 | 法务风险 | P2 且需合规结论；P0/P1 人工粘贴出处 | 合规未通过 → 不做 W11 |
| R6 | 小团队维护面扩大 | 交付变慢 | 内聚演进（§2），P0 不新增服务 | 新增服务 >2 个 → 重新评估 |
| R7 | 规则包形同虚设（只写不读） | 假合规 | 校验器 + prompt 指纹验证被真实引用 | 连续 2 个需求未引用 → 收缩规则范围 |

---

## 10. 里程碑与决策点

| 里程碑 | 含 | DoD（可判定 + 证据） | 决策 |
|---|---|---|---|
| **M0**（S0 末） | WP0 | CI 7 服务全绿；基线 234 记录 | 进入 P0 |
| **M1**（S4 末） | WP1–WP5 | E1 真拦 ≥1 次；来源反查链路 1 条；证据复验 3/3；测试 ≥280 | **重估估时**；决定 P1 范围（是否砍 WP9/WP10） |
| **M2**（S7 末） | WP6–WP10 | 岗位连续运行 ≥3 天（含一次断点恢复）；越权被工具层拒绝 ≥1 次；驾驶舱提交一次决策 | 决定 P2 与是否扩岗位 |
| **M3**（按需） | WP11/WP12 | 合规通过 + 复用率实测报告 | 决定是否推广 |

---

## 附录 A · 可直接当 Issue 的任务清单

**WP0**：`ci.yml` 补 2 服务 ▢ / 规则包校验 step ▢ / `sop.py` 注释纠正 ▢
**WP1**：`SourceRef` Schema ▢ / PG 迁移 ▢ / 澄清节点 ▢ / 模糊度检测 ▢ / 默认值确认卡 ▢ / 反查接口 ▢ / 单测 ▢ / 真实演练留证 ▢
**WP2**：`gate_rules.py` ▢ / `eng_gates.py` + 2 端点 ▢ / `gate_checks` 表 ▢ / `GateAPI` 客户端 ▢ / coder 前置拦截 ▢ / E1 规则 YAML ▢ / 4 项单测 ▢ / 真实拦截留证 ▢
**WP3**：E2/E3 规则 ▢ / 设计节点接入 ▢ / task-center 迁移前校验 ▢ / 打回与拒交付演练 ▢
**WP4**：三层规则目录 ▢ / manifest ▢ / 五份模板 ▢ / 校验器 ▢ / prompt 注入 ▢ / 校验器反例测试 ▢
**WP5**：证据表 ▢ / 上传下载端点 ▢ / E3 绑定校验 ▢ / hash 校验 ▢ / 复验演练 ▢
**WP6**：`roles.py` ▢ / 3 个岗位资产 ▢ / 校验测试 ▢
**WP7**：PG checkpointer ▢ / scheduler 进程 ▢ / 等待唤醒语义 ▢ / `agent_runs` 归档 ▢ / kill -9 恢复演练 ▢
**WP8**：工具注册表 ▢ / 鉴权中间件 ▢ / 包装既有 4 类工具 ▢ / MCP client ▢ / 越权拒绝演练 ▢
**WP9**：`decision_broadcast` ▢ / KB 权威条目 ▢ / 3 岗位分发验证 ▢
**WP10**：待决策队列 ▢ / 深链 ▢ / 决策提交 ▢ / 面板改造 ▢
**WP11/WP12**：飞书订阅 ▢ / ASR ▢ / OCR ▢ / Excel ▢ / 复用率报告 ▢ / Computer Use 沙箱 ▢

## 附录 B · 与 16 号文档的对应

| 16 号缺口 | 落点工作包 |
|---|---|
| D2 工程闸门缺失 | WP2、WP3 |
| D4 来源链 | WP1 |
| D3 规则粒度 | WP4 |
| 证据形态 | WP5 |
| D5 运行时形态 | WP6、WP7 |
| D6 工具接入 | WP8 |
| 决策分发 | WP9 |
| D7 人工介入形态 | WP10 |
| D1/D8 项目资产与验收对象 | WP4、WP12 |
| 多模态入口 | WP11 |

## 附录 C · 估时汇总

| 阶段 | 工作包 | 估时（人日） |
|---|---|---|
| P0 | WP0（1）+ WP1（5–7）+ WP2（4–6）+ WP3（5–7）+ WP4（6–8）+ WP5（5–7） | **26–36** |
| P1 | WP6（3–4）+ WP7（8–12）+ WP8（8–12）+ WP9（3–4）+ WP10（5–7） | **27–39** |
| P2 | WP11（8–12，含外部等待）+ WP12（5–8） | **13–20** |

**关键路径**：WP1 → WP2 → WP3 → WP5（E3 依赖证据表）；WP4 可与 WP1/WP2 并行；P1 关键路径 WP7 → WP8 → WP10。
**估时假设**：1 名熟悉本仓库的主力工程师；不含外部审批等待（飞书权限、合规）；协作评审按主力工时 20% 计入。**M1 必须重估**——本计划的所有数字都是估算，不是承诺。
