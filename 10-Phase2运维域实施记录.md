# 10 · Phase 2 运维域实施记录（告警诊断 → G5 变更审批 → 执行 → 复盘 G6）

> 目标（02 §4 / 05 Phase 2）：告警诊断 → 分级处置 → G5 变更审批 → 变更执行 → 复盘文档 → G6 复盘审查。

## 1. 交付内容（开发侧完成）

### 运维域 SOP（agent-runtime 新增 `ops-domain/alert-to-postmortem`）
```
diagnose(LLM: 告警+上下文+Runbook → 诊断 JSON) → change_prepare(分级: fix_code / operational)
  → g5_approval(GATE_CHANGE_APPROVAL, L3 生产强制审批) → gate_g5 ⏸
  → execute(fix_code: 修复分支+MR 合入 / operational: 执行记录+变更单)
  → postmortem(LLM 复盘: 时间线/根因/影响/改进项) → gate_g6 ⏸(GATE_POSTMORTEM_REVIEW)
  → approve: 闭环 / reject: 关闭
```
- 诊断输出结构化 JSON（root_cause/impact/change_type/change_summary/risk_level），宽松解析容错；
- 分级处置：fix_code 走 AI 修复（only_src + 最小修改约束）；operational 创建真实变更记录单（change/auto-ops 标签）；
- G5 三要素：诊断结论 + 修复分支/变更方案 + 风险自评（L3 生产变更，超时默认拒绝）；
- G6 复盘审查：复盘文档随审批上下文。

### Qwen 模型接入（LiteLLM 网关）
- `qwen-max` / `qwen-plus` 注册（DashScope OpenAI 兼容端点），`QWEN_API_KEY` 环境变量注入；
- 实测：经网关调用 qwen-max 返回正常（运维域演示全程由 Qwen 驱动）。

### 编码器增强（外科手术式编辑）
- prompt 附带 src 文件全文（≤10KB）+ "禁止占位注释替换函数体"约束——修复类任务质量显著提升；
- agent-runtime 建单/关单 API 增加 5xx/网络重试（代理抖动环境）。

## 2. 测试证据（全平台 66 项）

approval-gateway 17 · gitlab-bridge 17 · task-center 9 · agent-runtime 19（产品 6 + 研发 4 + 测试域 5 + **运维域 4**：修复合入全流程/操作变更建单/G5 否决/G6 否决）· knowledge-base 4。

## 3. 真实环境验证（gitlab.com · Qwen · 飞书）

`scripts/demo_ops.py --gateway --pg`（DEMO_LLM_MODEL=qwen-max）：**演示 10/10 通过**——
告警诊断 → G5（L3 三要素，飞书卡片）→ 批准 → 执行（真实变更记录单 #45）→ AI 复盘（时间线/根因/改进项）→ G6 批准 → 闭环；审批审计 + 卡片推送全绿。

### 修复过程中的工程问题（全部有据）
1. Qwen 修复重写 export/exporter 引入循环 import → 演示场景改为单文件 + 最小修改约束；
2. 模型把函数体替换为占位注释 → coder prompt 附带文件全文（外科手术式编辑）；
3. 代理抖动导致建单 502 → bridge/agent-runtime 重试；
4. **OpsState 缺 project_id/task_ref/title 字段**（LangGraph 丢弃未声明键 → 误用默认路径）→ 补字段。

## 4. 已知限制（诚实标注）

| 限制 | 影响 | 兜底 |
|---|---|---|
| fix_code 自动修复受模型外科编辑能力限制（DeepSeek/Qwen 均曾失败） | 复杂修复需人工 | G5 审批上下文含修复分支与测试结果；失败留痕人工接管 |
| Runbook 为输入文本（知识库 RAG 未接入运维域） | 诊断依据有限 | 后续接入 knowledge-base 检索 |
| 操作类执行（重启/扩容）为记录式（无真实基础设施） | 演示级 | 平台预留执行器接口 |

## 5. 剩余事项

| 事项 | 说明 |
|---|---|
| Runbook 接入知识库 | 告警诊断时 RAG 检索 Runbook（knowledge-base 已就绪） |
| 真实监控接入 | Prometheus/Grafana 告警 → 平台（webhook 事件 `alert.opened`） |
| 操作类执行器 | 对接云 API 真实执行重启/扩容（当前记录式） |
