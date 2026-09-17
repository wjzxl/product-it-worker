"""工程闸门缺口渲染（agent-runtime 侧，与 approval-gateway 的 render_gaps_text 同构）。

为什么在两边各有一份：agent-runtime 与 approval-gateway 是独立部署单元，
不应为了一行渲染而互相导入；两边的输出结构由 `contracts/gate-check.schema.json` 约束，
并用各自测试保证一致（见 tests/test_eng_gate_hook.py 的字段断言）。
"""
from __future__ import annotations


def render_gaps(result: dict) -> str:
    if result.get("passed"):
        return f"{result.get('code', 'E')} 闸门：全部检查项通过"
    tail = "已阻断推进" if result.get("blocked") else f"mode={result.get('mode')}，仅记录未阻断"
    lines = [f"{result.get('code', 'E')} 闸门未通过（{tail}）："]
    for gap in result.get("gaps") or []:
        mark = "⛔" if gap.get("severity") == "critical" else "•"
        lines.append(f"  {mark} {gap.get('ask')} → {gap.get('detail')}")
        if gap.get("hint"):
            lines.append(f"     建议：{gap['hint']}")
    missing = result.get("critical_missing") or []
    if missing:
        lines.append(f"  关键项缺失：{', '.join(missing)}（关键未知不能被多数通过抵消）")
    return "\n".join(lines)
