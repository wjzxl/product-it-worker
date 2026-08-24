"""任务状态机（对齐设计文档 02 §0）。

    pending ──> running ──> waiting_approval ──> running ──> done
                  │  │            │
                  │  └── failed <──┘ (可重跑)
                  └── done / failed
    waiting_approval ──> rejected（否决关闭）
"""
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running"},
    "running": {"waiting_approval", "done", "failed"},
    "waiting_approval": {"running", "rejected"},
    "failed": {"running"},
}

TERMINAL = {"done", "rejected"}


def validate_transition(current: str, target: str) -> bool:
    return current in TRANSITIONS and target in TRANSITIONS[current]


def describe(current: str, target: str) -> str:
    return f"{current} -> {target}"
