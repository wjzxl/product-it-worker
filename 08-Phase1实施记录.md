# 08 · Phase 1 实施记录（产品+研发闭环 MVP）

> 目标（05 路线图 Phase 1）：打通价值最密集的链路——需求→PRD→G1→Issue→技术方案→G2→AI 编码→质量门禁→MR→G3→合入，在真实仓库试点。

## 1. 本阶段交付（开发侧已完成）

### 研发域 SOP（agent-runtime 新增）
```
Issue → tech_design(DeepSeek 技术方案) → G2 评审 ⏸ → AI 编码(真实 DeepSeek) →
      质量门禁(pytest 真实执行,失败自动修复≤3轮) → 推送分支 → 建 MR →
      G3 评审 ⏸(附 MR 链接) → 合入(GitLab API, 带 sha) → done
```
- 挂起/恢复：G2、G3 双人审 Gate（LangGraph interrupt，同一线程可多次恢复）；
- 分支策略：`feat/issue-{iid}`；MR 幂等（返工复用已有 MR，不重复创建）；
- 保护机制：AI 不允许覆盖 `pyproject.toml` 等配置文件；质量门禁失败自动带测试输出迭代修复；
- 兼容修复：GitLab 新版合入 API 必须携带 `sha`（先查 MR 取源分支头提交）。

### 编码执行器（`agent-runtime/app/coder.py`）
- clone 仓库 → LLM 生成 `{"files": {relpath: content}}` → 应用 → pytest 门禁 → 迭代修复 → 提交推送；
- 经 LiteLLM 网关调用真实模型（DeepSeek deepseek-chat）；token 走环境变量不落盘；
- 诚实标注：宿主直接执行 git/python，容器沙箱隔离列入 Phase 2 加固。

### GitLab 集成桥扩展
- 新增：创建 MR / 查询 MR / 合入 MR（带 sha）端点与客户端，16 测试。

### 演示与工具
- `scripts/seed_dev_repo.py`：向真实仓库写入试点基线（报表查询模块 + 既有测试）；
- `scripts/demo_phase1.py`：研发域闭环端到端演示（12 项检查）；
- `scripts/batch_pilot.py`：批量试点（真实 Issue 自动触发 → 自动审批（记录决策耗时）→ 合入 → 质量报告，输出到 `platform/reports/`）。

### 自动触发闭环（task-center）
- GitLab Issue 带 **`auto-dev` 标签** → task-center 自动调用 agent-runtime 研发域 SOP（`AGENT_RUNTIME_URL` 配置后生效，fire-and-forget）——真实部署下新建 Issue 即自动进入流水线；本地无公网可达地址时由演示/批处理脚本模拟 webhook 投递。

## 2. 真实环境验收证据（2026-08-25）

| 检查项 | 结果 |
|---|---|
| Issue webhook → 任务中心 | ✅ |
| 研发域启动 → DeepSeek 技术方案 → G2 挂起（飞书卡片） | ✅ |
| G2 批准 → **AI 编码**（真实 DeepSeek 在真实仓库写代码） | ✅ |
| **质量门禁：pytest 真实执行**（7 个测试通过） | ✅ |
| 推送分支 + **真实 MR #3 创建** | ✅ |
| G3 挂起（附 MR 链接）→ 批准 | ✅ |
| **合入：GitLab MR #3 state=merged** | ✅ |
| 任务时间线（G2/G3 双审批留痕）+ 飞书卡片 0 失败 | ✅ |
| **演示合计：12/12** | ✅ |

- 真实产物：`sandbox6853335/prodesgin-demo` main 分支已合入 AI 代码（`src/reports/exporter.py` + 测试）；MR #1/#2 为演练残留，已关闭。
- 链接：[MR #3](https://gitlab.com/sandbox6853335/prodesgin-demo/-/merge_requests/3)

## 3. 批量试点（真实 Issue 端到端）✅

**10 个真实 Issue 全链路合入**（试点 9 + 演示 1）：MR #4~#8、#10~#13 全部 merged；测试规模 2 → 95；
平均端到端 56.3s；G2 决策平均 3.3s、G3 决策平均 0.53s（目标 <5 分钟）；
含 1 次真实"退回修订→再审批"演练（#20）与单元级 reject/revise 覆盖。

详见 [platform/reports/phase1-pilot-summary.md](platform/reports/phase1-pilot-summary.md)。

## 4. 测试证据（全平台 56 项）

approval-gateway 17 · gitlab-bridge 16 · task-center 9（含 auto-dev 自动触发）· agent-runtime 10（产品域 6 + 研发域 4）· knowledge-base 4。

## 5. 剩余事项（Phase 2 衔接）

| 事项 | 说明 |
|---|---|
| **沙箱容器隔离（加固）** | coder 当前宿主直跑；Phase 2 加固项 |
| 真实 webhook 回传 | GitLab→平台需公网可达地址（ngrok/云部署），属部署期事项 |
| "2 次真实退回修订"演练补足 | 已 1 次真实 + 单元覆盖；运营中自然积累即可 |
| 度量面板 | 将试点指标接入平台度量面板（Phase 2 度量体系） |

**下一步建议**：Phase 2 测试域+运维域接入（用例生成/缺陷闭环、告警诊断/变更审批），并落地编码沙箱隔离。
