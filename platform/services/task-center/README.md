# task-center · 平台任务中心（T0.5）

> 统一任务状态机 + 时间线事件 + 平台事件接入（幂等收件箱）。
> 状态机对齐设计文档 [02 §0](../../../02-核心SOP流程设计.md)，事件契约见 [contracts/events.md](../../contracts/events.md)。

## 快速开始

```bash
cd platform/services/task-center
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8002   # API 文档 http://localhost:8002/docs
pytest
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | 创建任务（external_ref 唯一，重复返回已有任务） |
| GET | `/tasks?status=` | 任务列表 |
| GET | `/tasks/{id}` | 任务详情（含完整时间线） |
| POST | `/tasks/{id}/transition` | 状态流转（状态机校验，非法流转 409） |
| POST | `/tasks/{id}/events` | 追加时间线事件（Agent 步骤留痕） |
| POST | `/events` | 平台事件接入（幂等收件箱，重复投递跳过） |

## 状态机

```
pending → running → waiting_approval → running → done
            │  │         │
            │  └─ failed ←┘（可重跑）
            └─ done / failed
waiting_approval → rejected（否决关闭）
```

## 事件联动（POST /events）

| 入站事件 | 动作 |
|---|---|
| `gitlab.issue.opened` | 创建任务（external_ref = `project_id#iid`，sop = product-domain/prd-flow） |
| `approval.requested`（含 task_ref） | 任务 running → waiting_approval |
| `approval.decided`（含 task_ref） | approve/revise → running；reject → rejected；时间线留痕（含决策人与原因） |
| 其他（含 task_ref） | 仅追加时间线 |
| 其他（无 task_ref） | 忽略并记录日志 |

**幂等**：信封 `event_id` 写入收件箱，重复投递直接跳过（GitLab 重试安全）。

## 联动配置

在 gitlab-bridge / approval-gateway 中设置 `PUBLISH_TARGETS=http://localhost:8002/events`，
即可把 GitLab 事件与审批决策实时汇入任务中心。
