# agent-runtime · Agent 运行时（T0.7）

> LangGraph 状态图实现产品域 SOP：澄清 → 起草 PRD → **G1 人审挂起** → 建 Issue。
> 流程定义对齐 [contracts/sop-example.yaml](../../contracts/sop-example.yaml)。

## 快速开始

```bash
cd platform/services/agent-runtime
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8003   # API 文档 http://localhost:8003/docs
pytest
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:4000/v1` | LiteLLM 网关地址 |
| `LLM_API_KEY` | 空 | 网关 master key；**为空时用 TemplateLLM 桩**（可离线跑通全流程） |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名（网关已注册） |
| `APPROVAL_GATEWAY_URL` | `http://localhost:8000` | 审批网关 |
| `GITLAB_BRIDGE_URL` | `http://localhost:8001` | GitLab 集成桥 |

## 流程与挂起语义

```
START → clarify → draft_prd ──(发起 G1 审批，三要素)──→ gate_g1 ⏸ interrupt
gate_g1 ──approve──> create_issues → END
gate_g1 ──revise───> draft_prd（按人审意见修订，重新发起审批）
gate_g1 ──reject───> reject_end → END
```

- 挂起/恢复由 LangGraph `interrupt()` + checkpoint 保证：进程重启后 `GET /runs/{id}/state` 仍可恢复；
- Phase 0 使用 MemorySaver；生产换持久化 checkpointer（Postgres/Redis）；
- 审批单创建后由审批网关发布 `approval.requested/decided` 事件，任务中心自动更新任务状态。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/runs` | 启动流程（thread_id = task_ref，多次启动同 task_ref 即恢复既有线程） |
| POST | `/runs/{thread_id}/resume` | 人审后恢复：{decision: approve/reject/revise, comment} |
| GET | `/runs/{thread_id}/state` | 查询状态（含中断值与状态快照） |
| GET | `/health` | 健康检查 |

## 与演示流的衔接

Phase 0 演示脚本（`platform/scripts/demo_phase0.py`）驱动三步：
1. POST `/runs`（模拟 GitLab Issue 事件后的任务启动）；
2. 人在审批台/API 完成 G1 决策；
3. POST `/runs/{id}/resume` 恢复执行。
Phase 1 起，`approval.decided` 事件由工作流引擎（n8n/Temporal）消费并自动触发第 3 步。
