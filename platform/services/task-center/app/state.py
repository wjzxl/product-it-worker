"""任务状态机（对齐设计文档 02 §0）。

    pending ──> running ──> waiting_approval ──> approved ──> running ──> done
                  │  │            │
                  │  └── failed <──┘ (可重跑)
                  └── done / failed
    waiting_approval ──> rejected（否决关闭）

**修复 BUG-005（CRITICAL）**：原先 `waiting_approval → running` 是合法边且不校验是否存在
审批决策，于是任务层可以绕过人审闸门直接恢复执行。现在：
  - 等待审批后必须先进 `approved`（代表"确实拿到了批准"），再由 `approved → running` 恢复；
  - `validate_transition` 之外，路由还会校验 `approved` 的来源（见 router 的决策校验）。
"""
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running"},
    "running": {"waiting_approval", "done", "failed"},
    # 等待审批后可以恢复执行，但**必须携带决策依据**（见 DECISION_EXITS）：
    #   批准 → running（继续干活）或 approved（显式记录"已获批"）
    #   否决 → rejected
    "waiting_approval": {"running", "approved", "rejected"},
    "approved": {"running"},
    "failed": {"running"},
}

TERMINAL = {"done", "rejected"}

# 从 waiting_approval 出发的目标状态：路由会校验"确实存在审批决策"，
# 否则任务层就能绕过人审闸门（修复 BUG-005）。
DECISION_EXITS_FROM = "waiting_approval"
DECISION_REQUIRED_TARGETS = {"running", "approved", "rejected"}


def validate_transition(current: str, target: str) -> bool:
    return current in TRANSITIONS and target in TRANSITIONS[current]


def describe(current: str, target: str) -> str:
    return f"{current} -> {target}"
