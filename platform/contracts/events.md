# 事件总线消息契约

> 所有组件通过事件解耦。事件走 Redis Streams/NATS（Phase 0 可先用 Redis Streams 或直接 HTTP 扇出）。
> 命名规范：`域.对象.动作`；每个事件必须携带 `trace_id` 用于 Langfuse 全链路关联。

## 消息信封

```json
{
  "event_id": "evt_01J...",
  "event_type": "gitlab.issue.opened",
  "occurred_at": "2026-08-25T10:00:00Z",
  "source": "gitlab-webhook",
  "trace_id": "trace_01J...",
  "payload": { }
}
```

## 入站事件（外部系统 → 平台）

| event_type | 触发 | 关键 payload 字段 |
|---|---|---|
| `gitlab.issue.opened` | 新建 Issue | `project_id, iid, title, description, labels` |
| `gitlab.issue.updated` | Issue 更新 | `project_id, iid, changes` |
| `gitlab.mr.opened` | 新建 MR | `project_id, iid, source_branch, target_branch` |
| `gitlab.mr.merged` | MR 合入 | `project_id, iid, merge_commit_sha` |
| `gitlab.pipeline.finished` | CI 完成 | `project_id, pipeline_id, status` |
| `im.card.callback` | IM 卡片按钮点击 | `callback_id, action(approve/reject/revise), operator, comment` |
| `chat.intake` | 聊天入口新需求 | `channel, sender, text` |

## 平台内部事件

| event_type | 触发 | 关键 payload 字段 |
|---|---|---|
| `task.created` | 任务创建 | `task_id, issue_ref, sop, params` |
| `task.claimed` | Agent 领取任务 | `task_id, agent, started_at` |
| `task.status_changed` | 状态流转 | `task_id, from, to, by` |
| `workflow.step.started / finished / failed` | 工作流步骤 | `task_id, step_id, inputs_ref, outputs_ref` |
| `approval.requested` | 流程到达 Gate | `approval_id, gate, task_id` |
| `approval.decided` | 人做出决策 | `approval_id, decision, decided_by, comment` |
| `approval.timeout` | 审批超时 | `approval_id, policy_applied` |
| `artifact.created` | 产物落库（PRD/方案/报告） | `artifact_ref, kind, version, links` |

## 出站事件（平台 → 外部系统）

| event_type | 目标 | 关键 payload 字段 |
|---|---|---|
| `gitlab.issue.create` | GitLab API | `project_id, title, description, labels` |
| `gitlab.mr.comment` | GitLab API | `project_id, iid, body` |
| `im.card.send` | IM 机器人 | `receivers, card_payload` |
| `cicd.trigger` | 现有 CI/CD | `pipeline_ref, params` |

## 约定

1. 消费方必须**幂等**（以 `event_id` 去重）；
2. `payload` 不可变，修正需发新事件并引用 `event_id`；
3. 审批类事件保留至少 3 年（审计要求，见 `03` §4）；
4. 所有事件写入平台审计日志（双轨审计的 AI 动作轨）。
