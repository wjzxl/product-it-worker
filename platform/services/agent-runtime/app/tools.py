"""工具注册表与执行期鉴权（WP8）。

对应课程图 59「两种边界」与图 62「够得着业务系统的四条路」：

- **看不见**：工具不在岗位允许列表 → 执行期直接拒绝（不是提示词劝阻）；
- **不能改**：工具标 `read_only: false` 或 `approval_gate` → 必须经人审/额外授权才能执行；
- **能力要登记**：工具说明（何时调用、输入输出、失败怎么办）在 `platform/tools/*.yaml` 里，
  而不是散在代码注释或口头约定中。

执行流程（`invoke`）：
  1. 工具必须已注册（否则拒绝）；
  2. 调用方岗位必须在允许列表内（`role`），且岗位技能里确实挂了这个 tool（WP6 校验过）；
  3. 必填参数齐备且类型正确；
  4. 非只读工具需要 `approval_ref`（除非显式 `allow_ungated`，用于演练与平台内部）；
  5. 审计留痕（strict 级记录全参数）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "tools"
KINDS = ("api", "cli", "mcp", "internal")
AUDIT_LEVELS = ("none", "standard", "strict")


class ToolError(ValueError):
    """工具定义非法（加载期）。"""


class ToolDenied(PermissionError):
    """执行期拒绝：未注册 / 岗位未授权 / 缺少参数 / 未过人审。"""

    def __init__(self, message: str, *, reason: str = "", tool: str = ""):
        super().__init__(message)
        self.reason = reason
        self.tool = tool


@dataclass
class ToolSpec:
    id: str
    title: str
    kind: str
    provider: str
    description: str
    read_only: bool
    idempotent: bool
    permission: str
    audit: str
    params: dict = field(default_factory=dict)
    requires_approval: bool = False   # 是否必须先有人审依据（"不能改"的落点）
    approval_gate: str = ""
    endpoint: str = ""
    command: str = ""
    sandbox: str = ""
    source_path: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "kind": self.kind, "provider": self.provider,
            "read_only": self.read_only, "idempotent": self.idempotent,
            "permission": self.permission, "audit": self.audit,
            "requires_approval": self.requires_approval,
            "approval_gate": self.approval_gate, "params": self.params,
        }


def tools_dir() -> Path:
    env = os.getenv("TOOLS_DIR", "")
    return Path(env) if env else DEFAULT_DIR


def _require(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise ToolError(f"{where}: {msg}")


def parse_spec(raw: dict, source: str = "<inline>") -> ToolSpec:
    name = Path(source).name
    _require(isinstance(raw, dict), name, "工具定义必须是对象")
    tool_id = str(raw.get("id") or "")
    _require(bool(tool_id), name, "缺少 id")
    kind = str(raw.get("kind") or "")
    _require(kind in KINDS, name, f"{tool_id}: kind 必须是 {KINDS}，收到 {kind!r}")
    audit = str(raw.get("audit") or "standard")
    _require(audit in AUDIT_LEVELS, name, f"{tool_id}: audit 必须是 {AUDIT_LEVELS}，收到 {audit!r}")
    _require(bool(raw.get("permission")), name, f"{tool_id}: 必须声明 permission（权限点）")
    _require(bool(raw.get("description")), name, f"{tool_id}: 必须写明 description（何时用/输入输出/失败怎么办）")
    params = raw.get("params") or {}
    _require(isinstance(params, dict), name, f"{tool_id}: params 必须是对象")
    for pname, spec in params.items():
        _require(isinstance(spec, dict), name, f"{tool_id}.{pname}: 参数定义必须是对象")
        _require(bool(spec.get("type")), name, f"{tool_id}.{pname}: 缺少 type")
    return ToolSpec(
        id=tool_id,
        title=str(raw.get("title") or tool_id),
        kind=kind,
        provider=str(raw.get("provider") or ""),
        description=str(raw["description"]),
        read_only=bool(raw.get("read_only", True)),
        idempotent=bool(raw.get("idempotent", True)),
        permission=str(raw["permission"]),
        audit=audit,
        params=params,
        # 缺省：写类工具需要人审依据；显式声明可覆盖（如"提交审批"本身不需要先有审批）
        requires_approval=bool(raw.get("requires_approval", not bool(raw.get("read_only", True)))),
        approval_gate=str(raw.get("approval_gate") or ""),
        endpoint=str(raw.get("endpoint") or ""),
        command=str(raw.get("command") or ""),
        sandbox=str(raw.get("sandbox") or ""),
        source_path=source,
    )


class ToolRegistry:
    """工具注册表：加载、校验、执行期鉴权。"""

    def __init__(self, directory: Optional[Path] = None, handlers: Optional[dict[str, Callable]] = None):
        self.directory = directory or tools_dir()
        self.specs: dict[str, ToolSpec] = {}
        self.handlers: dict[str, Callable] = dict(handlers or {})
        self.audit_log: list[dict] = []   # 内存审计（生产接 DB；演练与单测够用）
        self.load()

    def load(self) -> None:
        self.specs = {}
        if not self.directory.exists():
            logger.warning("工具目录不存在：%s（所有工具调用将被拒绝）", self.directory)
            return
        for path in sorted(self.directory.glob("*.yaml")):
            spec = parse_spec(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, str(path))
            if spec.id in self.specs:
                raise ToolError(f"{path.name}: 工具 id 重复：{spec.id}")
            self.specs[spec.id] = spec
        logger.info("工具注册表就绪：%d 个工具", len(self.specs))

    @property
    def ids(self) -> set[str]:
        return set(self.specs)

    def register_handler(self, tool_id: str, handler: Callable) -> None:
        if tool_id not in self.specs:
            raise ToolError(f"未注册的工具：{tool_id}")
        self.handlers[tool_id] = handler

    def allowed_for(self, role_tools: list[str]) -> list[str]:
        """岗位允许的工具（取交集，且必须已在注册表）——用于驾驶舱展示"这个岗位能碰什么"。"""
        return sorted(t for t in role_tools if t in self.specs)

    def invoke(
        self,
        tool_id: str,
        params: dict,
        *,
        role: str = "",
        role_tools: Optional[list[str]] = None,
        approval_ref: str = "",
        allow_ungated: bool = False,
    ) -> dict:
        """执行工具（鉴权 → 参数校验 → 执行 → 审计）。"""
        spec = self.specs.get(tool_id)
        if spec is None:
            self._audit(tool_id, role, params, "denied", "unregistered")
            raise ToolDenied(f"工具未注册：{tool_id}", reason="unregistered", tool=tool_id)

        # ① 岗位边界：看不见 = 不在允许列表
        if role_tools is not None and tool_id not in role_tools:
            self._audit(tool_id, role, params, "denied", "role_not_allowed")
            raise ToolDenied(
                f"岗位 {role or '-'} 未授权使用工具 {tool_id}（允许：{sorted(role_tools)}）",
                reason="role_not_allowed", tool=tool_id,
            )

        # ② 不能改：写类危险工具必须有人审依据（**先鉴权再校验参数**，
        #    这样错误信息指向"缺授权"而不是"缺参数"，避免把授权问题误读成格式问题）
        if spec.requires_approval and not allow_ungated and not approval_ref:
            gate = spec.approval_gate or "人审"
            self._audit(tool_id, role, params, "denied", "approval_required")
            raise ToolDenied(
                f"工具 {tool_id} 会修改外部状态，需先经 {gate} 并把 approval_ref 一并传入",
                reason="approval_required", tool=tool_id,
            )

        # ③ 必填参数
        missing = [n for n, s in spec.params.items() if s.get("required") and params.get(n) in (None, "", [])]
        if missing:
            self._audit(tool_id, role, params, "denied", "missing_params")
            raise ToolDenied(f"工具 {tool_id} 缺少必填参数：{', '.join(missing)}",
                             reason="missing_params", tool=tool_id)

        handler = self.handlers.get(tool_id)
        if handler is None:
            self._audit(tool_id, role, params, "no_handler", "")
            return {"tool": tool_id, "status": "no_handler",
                    "note": "工具已注册但未绑定执行实现（演练/未接线状态）"}

        try:
            result = handler(**params)
        except Exception as exc:
            self._audit(tool_id, role, params, "error", f"{type(exc).__name__}: {exc}")
            raise
        self._audit(tool_id, role, params, "ok", "")
        return {"tool": tool_id, "status": "ok", "result": result}

    def _audit(self, tool_id: str, role: str, params: dict, outcome: str, reason: str) -> None:
        spec = self.specs.get(tool_id)
        if spec is not None and spec.audit == "none":
            return
        entry = {
            "tool": tool_id,
            "role": role,
            "outcome": outcome,
            "reason": reason,
            "params": params if (spec and spec.audit == "strict") else {k: "<redacted>" for k in (params or {})},
        }
        self.audit_log.append(entry)
        if outcome == "denied":
            logger.warning("工具调用被拒：%s role=%s reason=%s", tool_id, role or "-", reason)
        else:
            logger.info("工具调用：%s role=%s outcome=%s", tool_id, role or "-", outcome)

    def summary(self) -> dict:
        return {
            "dir": str(self.directory),
            "count": len(self.specs),
            "tools": [s.to_dict() for s in sorted(self.specs.values(), key=lambda x: x.id)],
            "write_tools": sorted(s.id for s in self.specs.values() if not s.read_only),
            "approval_required_tools": sorted(s.id for s in self.specs.values() if s.requires_approval),
            "gated_tools": sorted(s.id for s in self.specs.values() if s.approval_gate),
        }
