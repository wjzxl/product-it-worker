# agent-runtime · Agent 运行时（T0.7 / Phase 1）

> LangGraph 状态图：**产品域** PRD + G1；**研发域** 技术方案 + G2 + AI 编码 + 质量门禁 + MR + G3 + 合入。
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
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 网关地址 / 空 / gpt-4o-mini | 为空时用 TemplateLLM 桩 |
| `APPROVAL_GATEWAY_URL` | `http://localhost:8000` | 审批网关 |
| `GITLAB_BRIDGE_URL` | `http://localhost:8001` | GitLab 集成桥 |
| `GITLAB_URL` / `GITLAB_TOKEN` | gitlab.com / 空 | 编码执行器 clone/push 用（研发域） |
| `DEMO_PROJECT` | `sandbox/prodesgin` | 编码目标项目路径（研发域） |

## 两条 SOP

### 产品域 `product-domain/prd-flow`
```
clarify → draft_prd → gate_g1 ⏸ → approve → create_issues → END
                              └─ revise → draft_prd（按意见修订）
                              └─ reject → reject_end
```

### 研发域 `dev-domain/issue-to-merge`
```
tech_design → gate_g2 ⏸ → approve → implement(AI 编码+pytest 门禁) → create_mr → g3_approval → gate_g3 ⏸
                        └─ revise → tech_design                    └─ revise → implement（返工复用 MR）
                                                                    └─ reject → reject_end
gate_g3 → approve → merge(带 sha) → END
        └─ reject → reject_end
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/runs` | 启动流程（`sop` 二选一；thread_id = task_ref 净化后幂等键） |
| POST | `/runs/{thread_id}/resume` | 人审后恢复：{decision, comment} |
| GET | `/runs/{thread_id}/state` | 状态快照（含中断值、G2/G3 上下文） |
| GET | `/health` | 健康检查 |

## 端到端验证

- `scripts/demo_phase1.py --gateway --pg`：研发域闭环演示（12 项检查，真实 GitLab + DeepSeek）；
- `scripts/batch_pilot.py --gateway --pg --count N`：批量试点（真实 Issue 自动触发 → 合入 → 质量报告）；
- 闭环触发：GitLab Issue 带 `auto-dev` 标签 → task-center 自动调用 `/runs`（sop=dev）。
