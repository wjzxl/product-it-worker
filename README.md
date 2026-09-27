# 产品研发智能化平台 · 总体方案

> 一条覆盖 **产品 → 研发 → 测试 → 运维** 的 AI 自动化生产线：**AI 是主力执行者，人是决策者与审查者。**

## 平台定位

传统研发工具链是人使用工具、流程约束人；本平台把整条 SOP 流程变成一条可编排、可审计、可度量的自动化生产线：

- 每个 SOP 步骤由 **角色化 AI Agent** 自动执行（起草 PRD、写技术方案、编码、生成并执行测试、诊断告警、写复盘……）；
- 流程中的 **决策点与审查点** 是显式的人审闸门（Gate），人在闸门上做批准、拒绝或带意见退回；
- 人的每一次否决都成为反馈信号，驱动 Prompt、知识库、SOP 与信任等级的持续进化。

## 输入画像（本项目按此设计）

| 维度 | 取值 | 对设计的影响 |
|---|---|---|
| 构建策略 | **组装为主**：成熟开源组件为核心，自研只做编排层与人审网关 | 落地快、风险低，重点选型与集成 |
| 团队规模 | **小团队（<20 人）**，单产品线 | 单租户即可，先做单链路闭环 |
| 现有工具 | **已有代码托管 + CI/CD**（GitLab 系） | 平台**接入而非替代**，GitLab 作为任务/代码事实源 |
| 部署环境 | **公有云** | 可直接使用云厂商 LLM API、托管服务与弹性算力 |

## 一句话设计原则

> **把"人做事、AI 帮忙"反转成"AI 做事、人把关"；把关点要少而准，把关上下文要完整，把关结果要回流。**

## 开发进度（随实施更新）

| 阶段 | 进度 |
|---|---|
| 设计（文档 01~09） | ✅ 完成，6 个 Gate 已评审确认 |
| **Phase 0 地基** | ✅ 全部完成并验收通过（真实 GitLab/飞书/DeepSeek、成本计量与观测闭环、CI 55 测试） |
| **Phase 1 产品+研发闭环 MVP** | ✅ **完成并验收**：研发域闭环（DeepSeek 编码+质量门禁+MR+G3+合入）+ 真实仓库 **11 个 Issue 端到端合入**（MR #4~#15，测试 2→115，含 2 次真实退回修订演练），试点报告见 [platform/reports/phase1-pilot-summary.md](platform/reports/phase1-pilot-summary.md) |
| **Phase 2 测试域** | ✅ 测试域流水线完成（独立用例→分层执行→缺陷闭环→G4 放行），批量试点 + 验收文档（[09](09-Phase2测试域实施记录.md)） |
| **Phase 2 运维域** | ✅ **运维域流水线完成**（告警诊断→G5 变更审批→执行→复盘→G6），Qwen 已接入，真实演示 10/10（[10](10-Phase2运维域实施记录.md)） |
| **Phase 2 收尾三项** | ✅ **全部完成**：Runbook RAG（Qwen 向量）+ Prometheus 告警接入（演示 10/10，AI 修复 MR !16 真实合入）；编码/测试沙箱容器隔离（`--network none` 实测）；三域度量面板 + [Phase 2 验收报告](12-Phase2验收报告.md)（75 测试全绿） |
| **生产化 ③ 真实 CI/CD + 云 API** | ✅ 完成：`.gitlab-ci.yml` + bridge 触发/回读端点 + 运维域 D1 发布节点；操作执行器 dryrun/webhook（[13](13-生产化与Phase3实施记录.md)） |
| **Phase 3 信任分级动态升降级** | ✅ 完成：L1 全自动 / L2 抽样 / L3 强制，失败即回升；**真实数据验证**（G3/G4 升 L1、G2 因 3 次真实退回降 L2），approval-gateway 31 测试 |
| **生产化 ① 公网 webhook / SSO / 多租户** | ✅ 完成：HMAC 签名 + webhook 自动注册（真实验证 #89070692）+ 生产编排（Caddy/HTTPS，config 校验通过）；OIDC SSO（真实 JWKS 验证 401/403/200 + 审计）；多租户隔离（PG 自动迁移 + 跨租户 404） |
| **Phase 3 流程挖掘与 SOP 自进化** | ✅ 完成：挖掘（Gate 等待/超时/退回/变体/成本）→ LLM 提案 → **G7 流程改进闸门** → 配置中心应用（热更新）→ GitLab 改进项台账；真实演示 **9/9**，全平台 **138 测试**（[14](14-Phase3流程挖掘与SOP自进化.md)） |
| **Phase 3 扩展三项** | ✅ 完成（[15](15-Phase3扩展实施记录.md)）：① Prompt 版本化注册中心 + 知识库自动入库（应用动作，热更新实测）；② **标准流程挖掘算法**（事件日志/DFG/Heuristic/α-Miner + 三种一致性检查，真实日志 dfg 留一法 fitness **0.97** 并命中唯一异常案例）；③ **多产品线推广**（信任按 `<tenant>::<GATE>` 隔离 + 产品线接入脚本 `onboard_product_line.py` + 隔离验收演练）+ **私有化/多模型评估夹具**（10 任务金标集 + 沙箱判分 + 报告）。全平台 **232 测试全绿** |
| Phase 2 测试+运维域 | ⏳ 未开始 |
| Phase 3 自进化 | ⏳ 未开始 |

**已可运行**：审批网关（17 测试）、GitLab 集成桥（16）、任务中心（9）、Agent 运行时（10）、知识库（4），共 56 测试；演示：`platform/services/agent-runtime/.venv/bin/python platform/scripts/demo_phase0.py`（产品域）、`demo_phase1.py`（研发域）、`batch_pilot.py`（批量试点）。

**多产品线接入 / 模型选型**：

```bash
cd platform
# 接入一条新产品线（含隔离验收演练）
services/approval-gateway/.venv/bin/python scripts/onboard_product_line.py \
    --tenant line-pay --product-name "支付中台" --profile strict --verify
# 多模型对比 / 私有化模型验收（EVAL_BASE_URL 指向任意 OpenAI 兼容端点）
SANDBOX_MODE=host services/agent-runtime/.venv/bin/python scripts/model_eval.py \
    --models deepseek-chat,qwen-plus --tag cloud
```

**本地起全栈（为收集闸门真实样本）**：

```bash
cd platform
services/agent-runtime/.venv/bin/python scripts/run_local_stack.py --dry-run   # 先看将启动什么
services/agent-runtime/.venv/bin/python scripts/run_local_stack.py             # 默认 ENG_GATES_MODE=warn
# 跑完一条真实需求后看样本与就绪度：
services/approval-gateway/.venv/bin/python scripts/gate_metrics.py
services/approval-gateway/.venv/bin/python scripts/gate_readiness.py           # 能否切 block
```

最新评估结论（[报告](platform/reports/model-eval-cloud-v4-metered-20260916-014327.md)）：云模型能力均已够用，选型差异主要在**成本与延迟**（deepseek-chat P50 1.9s、$0.0076；qwen-plus $0.0081；qwen-max $0.0291）；私有化小模型接入前用同一夹具验收（弱模型桩实测 0/5 被挡）。Qwen 成本已接入 LiteLLM 计量（原先 unmetered），历史缺口可用 `scripts/qwen_cost_audit.py` 复盘。

## 文档导航

| 文档 | 内容 |
|---|---|
| [01-总体设计与架构](01-总体设计与架构.md) | 愿景、双环模型、角色重定义、总体架构分层、一次典型流转、关键架构决策 |
| [02-核心SOP流程设计](02-核心SOP流程设计.md) | 产品/研发/测试/运维四域 SOP 详设、任务状态机、质量门禁、缺陷与变更闭环 |
| [03-人机协作与治理](03-人机协作与治理.md) | 角色模型、信任分级、审批网关、安全与审计、反馈闭环 |
| [04-技术选型与组件清单](04-技术选型与组件清单.md) | 组件选型对比、云上部署拓扑、成本模型、自研与组装边界 |
| [05-实施路线图与度量](05-实施路线图与度量.md) | 四阶段路线图、验收标准、度量体系、风险与对策 |
| [16-FDE交付范式对标与目标架构](16-FDE交付范式对标与目标架构.md) | 对标《从 vibe coding 到 FDE 交付》：相同/不同点、融合目标架构（双层闸门/来源链/项目规则包/工具注册表/岗位运行时/驾驶舱）、改动清单与可行性、验收标准 |
| [17-FDE范式落地开发计划](17-FDE范式落地开发计划.md) | 13 个工作包（WP0–WP12）：契约与数据模型、2 周迭代排期、测试与灰度策略、度量与 kill criteria、里程碑决策点、可直接当 Issue 的任务清单 |
| [18-M1复盘与P1决策](18-M1复盘与P1决策.md) | P0（WP0–WP5）复盘：交付对照、测试 234→329、6 个计划外缺陷、度量现状（生产样本 0 的诚实结论）、估时重估与 P1 五项决策 |
| [19-M2复盘与P1验收](19-M2复盘与P1验收.md) | P1（WP6–WP10）验收：岗位注册表/持久化运行时/工具鉴权/决策分发/驾驶舱全部留证；9 个 P1 缺陷；已验证 vs 未验证的诚实分界；下一步灰度动作 |
| [20-业务确认落地记录](20-业务确认落地记录.md) | E1 六项检查（3 critical，含两个豁免口）、模糊词表可配置化（B1–B4）、误拦反馈流程与可信度门槛（C1–C5）、warn→block 就绪度检查（D2）、临时默认值待确认清单 |
| [21-治理默认值取舍与部署档位](21-治理默认值取舍与部署档位.md) | 严格 vs 开发便利的取舍：按「失败后果是否可逆」分档；收拢到 `PLATFORM_ENV=dev|staging|prod` 一个旋钮；`/health` 暴露安全姿态、prod 缺凭据拒绝启动、上线体检脚本卡点；含 4 项不可协商清单与代价说明 |

## 建议阅读顺序

1. 先读 `01` 建立整体心智模型（10 分钟）；
2. 再读 `02` 看四个域的 SOP 流水线与每个 Gate 的定义（30 分钟）；
3. `03` 决定"人在哪里、怎么把关"——这是本平台与一般 AI 编码工具的本质差异；
4. `04`、`05` 用于立项、选型评审与排期。
