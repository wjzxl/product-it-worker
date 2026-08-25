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
- `scripts/demo_phase1.py`：研发域闭环端到端演示（12 项检查）。

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

## 3. 测试证据（全平台 55 项）

approval-gateway 17 · gitlab-bridge 16 · task-center 8 · agent-runtime 10（产品域 6 + 研发域 4：批准合入/方案修订/代码否决/返工复用 MR）· knowledge-base 4。

## 4. 剩余事项（Phase 1 正式验收门槛）

按 [05-实施路线图](05-实施路线图.md)，Phase 1 验收还需（属持续运营，非开发）：

| 事项 | 说明 |
|---|---|
| **试点 ≥10 个真实 Issue 端到端** | 演示已跑通单条闭环；需在试点项目批量跑 10+ issue（自动或半自动） |
| **质量指标不劣化** | 对比试点前后：逃逸缺陷、返工率、人审决策时间（<5 分钟/次） |
| 人审平均决策时间度量 | 审批台/卡片已支持，需运营数据 |
| 沙箱容器隔离（加固） | coder 当前宿主直跑；Phase 2 加固项 |

**下一步建议**：把"Issue 创建即自动触发研发域 SOP"接成闭环（task-center 监听 issue.opened → 自动调 agent-runtime 研发域流程，而非演示脚本手动编排），随后批量试点 10 个 Issue。
