# 12 · Phase 2 验收报告（测试域 + 运维域 + 收尾三项）

> 验收依据：[05-实施路线图](05-实施路线图.md) Phase 2（测试域全链、运维域全链、信任分级、度量面板）与 [02-核心SOP流程设计](02-核心SOP流程设计.md) 各域 SOP。
> 验收方式：全平台自动化测试 + 真实环境演示/批量试点 + 真实基础设施实测（GitLab / 飞书 / LiteLLM 网关 / Qdrant / PostgreSQL / Docker 沙箱）。
> 验收日期：2026-09（Phase 2 收尾）。状态：**开发与真实验证全部完成；遗留项见 §5**。

## 1. 交付清单

| 模块 | 内容 |
|---|---|
| 研发域（Phase 1 延续） | G2 方案评审 → AI 编码 → 质量门禁 → MR → G3 代码审查 → 合入 |
| **测试域** | 测试计划 → 独立用例生成（隔离自检）→ 分层执行（单元/集成）→ 缺陷闭环（缺陷单→AI 修复→回归→关单）→ 测试报告 → **G4 放行决策** |
| **运维域** | 告警诊断（**Runbook RAG**）→ 分级处置（fix_code/operational）→ **G5 变更审批** → 变更执行 → AI 复盘 → **G6 复盘审查** |
| 真实告警接入 | task-center `POST /alerts/prometheus`（Alertmanager webhook）→ 建任务 + 自动触发运维域 SOP |
| Runbook 知识库 | `platform/runbooks/` 3 篇 + Qwen 向量（text-embedding-v3, 1024 维）入库脚本 + 检索引用留痕 |
| **编码/测试沙箱** | `platform-sandbox` 镜像 + `--network none`/资源限额/非 root/用后即毁；coder 与 tester 统一经沙箱执行 |
| **度量面板** | `metrics-dashboard` 服务：Gate 决策/平均决策耗时/任务/LLM 成本（HTML 面板 + `/metrics` JSON） |
| 模型接入 | DeepSeek `deepseek-chat`、Qwen `qwen-max`/`qwen-plus`、`qwen-embed`（均经 LiteLLM 网关） |

## 2. 测试证据（全平台 **75 项**全绿）

| 服务 | 测试数 | 覆盖要点 |
|---|---|---|
| approval-gateway | 17 | 状态机、三要素、超时（L3 强制拒绝）、飞书卡片、审批台 |
| gitlab-bridge | 17 | webhook 归一化、GitLab API（Issue/MR/合入/关单）、事件扇出 |
| task-center | 11 | 状态机、时间线、幂等收件箱、审批联动、**auto-dev 自动触发**、**Prometheus 告警接入** |
| agent-runtime | 23 | 产品域 6 + 研发域 4 + 测试域 5 + **运维域 4** + **沙箱 4** |
| knowledge-base | 4 | 分块、入库、带来源检索 |
| metrics-dashboard | 3 | 指标结构、面板渲染、健康检查 |
| **合计** | **75** | 全部通过（Python 3.9；CI 矩阵自动回归） |

## 3. 真实环境验收结果

### 3.1 研发域（Phase 1 试点，真实仓库）
- **11 个真实 Issue 端到端合入**（MR #3~#8、#10~#13、#15），**2 次真实"退回修订→再审批"**（#20、#25）；
- 测试规模 2 → 115；平均端到端 67.1s；G2 决策 3.0s / G3 决策 0.49s。
- 详见 [platform/reports/phase1-pilot-summary.md](platform/reports/phase1-pilot-summary.md)。

### 3.2 测试域（批量试点 10 功能）
- 到达 G4 **9/10**；独立用例生成成功率 **9/9**；缺陷闭环机制真实触发 9 次；G4 决策平均 **0.7s**；
- 经验证 9 个缺陷单均为**生成用例质量偏差**（模型幻觉 API 签名），非产品缺陷；平台兜底正确（不误合入、留痕、人工接管）。
- 详见 [platform/reports/phase2-test-pilot-summary.md](platform/reports/phase2-test-pilot-summary.md)。

### 3.3 运维域 + 告警接入（Qwen 驱动）
| 演示 | 结果 |
|---|---|
| `demo_ops.py`（告警→诊断→G5→执行→复盘→G6） | ✅ **10/10** |
| `demo_alert.py`（Prometheus 接入 + Runbook RAG） | ✅ **10/10**，含 **AI 修复 MR !16 真实合入**（main 已含 365 天范围校验） |
| Runbook 检索 | ✅ top=export-performance.md（score 0.741），引用进入 G5 审批依据 |

### 3.4 沙箱隔离（真实实测）
- 容器内 `/.dockerenv` 存在（`IN_CONTAINER True`）+ **`NET_BLOCKED`（执行期无网络）** + 容器内 pytest 通过；
- 沙箱模式下跑通测试域演示（`demo_phase2.py` + `SANDBOX_MODE=docker`）7/7，日志确认 `沙箱模式：docker`。

### 3.5 三域汇总指标（PostgreSQL 实测，`scripts/metrics_report.py`）
| 指标 | 数值 |
|---|---|
| 审批单总数 / 任务总数 / 时间线事件 | **67 / 33 / 133** |
| 人审平均决策耗时 | **1.4s**（六大 Gate 均 < 2.5s，目标 < 5 分钟 ✅） |
| Gate 审批分布（数量/平均决策秒） | G1 4/0.6、G2 24/2.29、G3 15/0.6、G4 18/0.91、G5 4/1.94、G6 2/0.65 |
| 决策分布 | approve 64、revise 3（真实退回修订） |
| LLM 累计成本 | **$0.5152**（DeepSeek 174 次调用 / 542,657 tokens；Qwen 32 次调用未计量，见 §5） |

## 4. 验收对照

| 验收项（05 Phase 2） | 结果 |
|---|---|
| 测试域全链（用例→分层执行→缺陷闭环→放行报告 G4） | ✅ 完成（真实验证 + 批量试点） |
| 运维域全链（灰度/告警→G5 变更审批→复盘 G6） | ✅ 完成（发布执行编排待真实 CI/CD 接入，见 §5） |
| 信任分级（L1/L2/L3）+ 风险分 | 🔶 部分：L1/L2/L3 与风险分已在审批请求中落地（L3 生产变更强制审批、超时默认拒绝）；动态升降级（按历史指标自动调整）未实施 |
| 度量面板 | ✅ 完成（三域指标 + HTML 面板 + `/metrics`） |
| 知识库治理 | ✅ Runbook 入库 + 检索引用留痕 |
| 安全加固（沙箱） | ✅ 完成（执行环节容器隔离实测） |

## 5. 已知限制与后续项（诚实标注）

| 限制 | 影响 | 后续 |
|---|---|---|
| 模型外科手术式编辑能力有限（DeepSeek/Qwen 均曾失败；引入文件全文 + Runbook 约束后成功率显著提升） | 复杂修复可能失败 | 已兜底：Gate 审批留痕 + 人工接管；可换更强模型 |
| 独立用例生成质量不稳（幻觉 API 签名） | 用例需人工审核 | 生成侧加 AST 校验（已部分）；或强模型生成 |
| **Qwen 成本未计量** | 成本面板缺 Qwen 价格 | ✅ **已修复（Phase 3 ③-F）**：LiteLLM 显式单价（qwen-max $1.6/$6.4、qwen-plus $0.4/$1.2、text-embedding-v3 $0.07 每 1M tokens），实测响应头计量正确；历史 118 次调用缺口 $0.2438 由 `scripts/qwen_cost_audit.py` 量化（不回填），修正后平台 LLM 累计成本 ≈ $0.781 |
| 操作类变更执行为记录式 | 无真实基础设施动作 | 对接云 API 执行器 |
| git clone/push 仍在宿主执行 | 凭证暴露面 | 后续在沙箱网络侧加代理白名单 |
| 发布执行编排（D1/D2）依赖真实 CI/CD | 灰度发布未真机验证 | 接入现网流水线后验证 |
| 任务域归属统计口径 | 研发/测试域任务经 `/runs` 直连（未建 task-center 任务），"任务按域"仅反映产品/运维域 | 统一由 task-center 建任务 |

## 6. 验收结论

**Phase 2 通过验收**：产品→研发→测试→运维全链路六大 Gate（G1~G6）自动化生产线闭环成型；
三域 SOP 均经真实环境验证（真实 GitLab 仓库、真实模型 DeepSeek+Qwen、真实飞书卡片、真实告警接入、真实沙箱隔离、真实成本与观测）；
全平台 75 项测试全绿；度量面板可实时查看三域指标。

签核人：＿＿＿＿＿＿　日期：＿＿＿＿＿＿
