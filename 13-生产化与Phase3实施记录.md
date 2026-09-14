# 13 · 生产化与 Phase 3 实施记录

> 三项生产化：① 生产化部署（公网 webhook、SSO、多租户）；② 信任分级动态升降级（Phase 3）；③ 真实 CI/CD 与云 API 打通。

## ③ 真实 CI/CD + 云 API 操作执行器 ✅（已完成，见 [11 文档](11-Phase2收尾实施记录.md) ③' 节）

- 仓库 `.gitlab-ci.yml`（test→build）；gitlab-bridge 触发/列表/作业回读端点；运维域 D1 发布执行节点；
- `app/executors.py`：`dryrun` / `webhook`（真实 HTTP 下发运维 API）；
- 真实验证 `scripts/ci_check.py`：读流水线（failed/0 作业）✅、触发被 GitLab 实名验证拦截 ⚠️（诚实呈现，需账号实名或内网 GitLab）。

## ② 信任分级动态升降级（Phase 3）✅（本轮完成）

### 机制（`app/trust.py`）
| 等级 | 条件（滚动窗口最近 20 条决策） | 行为 |
|---|---|---|
| **L1 全自动** | 样本 ≥10 且通过率 ≥90% 且近 5 条无退回/否决 | Gate 自动放行，留痕 `system:trust` |
| **L2 抽样审批** | 通过率 ≥80%（不满足 L1） | 按 `TRUST_SAMPLE_RATIO`（默认 20%）抽样，命中才需人审 |
| **L3 强制审批** | 通过率 <80% 或存在生效惩罚 | 全部人工审批 |

- **失败即回升（降级单向门）**：任一 reject/revise 立即写入惩罚期（`TRUST_PENALTY_HOURS`，默认 24h），期内强制 L3；到期后按新指标重算（需指标自然回升才恢复 L1/L2）；
- **抽样确定性**：以 `sha256(approval_id)` 分桶，同一审批结果可复现（便于审计）；
- **接口**：`GET /trust`（全 Gate）、`GET /trust/{gate_type}`；`POST /approvals` 新增 `auto_gate` 字段（L1 放行 / L2 抽样 / L3 人工），返回 `trust_level`、`trust_reason`、`auto_decision`；
- 自动放行同样发布 `approval.decided` 事件（channel=trust），任务中心与度量面板同步可见。

### 真实验证（PostgreSQL 中 67 条真实审批）
| Gate | 真实等级 | 样本 | 通过率 | 说明 |
|---|---|---|---|---|
| G3 代码审查 | **L1** | 15 | 100% | 自动放行 |
| G4 放行决策 | **L1** | 18 | 100% | 自动放行 |
| G2 方案评审 | L2（抽样 20%） | 20 | **85%** | **因 Phase 1 的 3 次真实退回被降级** ✅ 动态降级生效 |
| G1 / G5 / G6 | L2（样本不足） | 2~4 | 100% | 样本 <10 不升 L1 |

真实自动放行实测：向 G3 提交 `auto_gate=true` 审批 → `status=approved`、`trust_level=L1`、`auto_decision=approved`，
留痕 `decided_by=system:trust`，理由："通过率 100%（15/15）且近 5 条无退回"。

### 测试（approval-gateway 共 **31 项**）
新增 14 项：指标→等级映射（L1/L2/L3、样本不足、近期退回阻断）、惩罚期与到期恢复、惩罚按 Gate 隔离、全量列举、抽样器（L1/L3/确定性/边界）、API 级自动放行 + 否决后降级 + `/trust` 端点。

## ① 生产化部署（公网 webhook、SSO、多租户）—— 待实施
