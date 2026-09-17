"""工程闸门规则引擎（WP2）：规则即代码，判定器可单测。

与 G1–G7 的区别（16/17 号文档「双层闸门」）：
  - **E 闸门是机器判定**：检查「实施条件是否具备」，规则写在 YAML 里，判定是纯函数；
  - **G 闸门是人做业务决策**：审批三要素 + 信任分级。
  E 管"能不能安全动手"，G 管"业务是否同意"。

三条硬要求（来自课程图 40 与计划 WP2）：
  1. **拒绝"只提示不阻断"**：`mode=block` 且存在 critical 缺口时，调用方必须停止推进；
  2. **关键未知不能被多数通过抵消**：4/5 项通过但 `severity=critical` 未答 → 仍然拒绝；
  3. **判定可复现**：同一上下文 + 同一规则版本 → 同一结论，故不依赖 LLM。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "engineering-gates"
SUPPORTED_MODES = ("off", "warn", "block")
SUPPORTED_REQUIRE = ("non_empty", "any_of", "all_of", "source_present", "evidence_ids_present")
SEVERITIES = ("critical", "major", "minor")


class GateRuleError(ValueError):
    """规则文件非法（报错要指明文件与检查项）。"""


@dataclass
class Check:
    id: str
    ask: str
    require: str = "non_empty"
    keys: list[str] = field(default_factory=list)
    severity: str = "major"
    hint: str = ""


@dataclass
class GateRule:
    code: str
    title: str
    mode: str
    checks: list[Check]
    version: str = ""
    description: str = ""
    source_path: str = ""

    def fingerprint(self) -> str:
        payload = yaml.safe_dump(
            {"code": self.code, "mode": self.mode,
             "checks": [{"id": c.id, "require": c.require, "keys": c.keys, "severity": c.severity} for c in self.checks]},
            sort_keys=True, allow_unicode=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _require(cond: bool, path: str, where: str, msg: str) -> None:
    if not cond:
        raise GateRuleError(f"{path} · {where}: {msg}")


def rule_dir() -> Path:
    env = os.getenv("ENGINEERING_GATES_DIR", "")
    return Path(env) if env else DEFAULT_DIR


def gate_mode(default: str = "warn") -> str:
    """读取全局灰度开关 `ENG_GATES_MODE`。

    - 未设置 → 返回 `default`（调用方传规则自身的 mode）；
    - 设置了非法值 → **按 warn 处理并告警**：该变量是灰度/熔断开关，
      配置笔误绝不能让流水线被硬阻断（阻断必须来自明确的意图）。
    """
    raw = (os.getenv("ENG_GATES_MODE", "") or "").strip().lower()
    if not raw:
        return default
    if raw in SUPPORTED_MODES:
        return raw
    logger.warning("ENG_GATES_MODE=%r 非法（可用 %s），按 warn 处理", raw, "/".join(SUPPORTED_MODES))
    return "warn"


def resolve_mode(rule_mode: str, override: Optional[str] = None) -> str:
    """最终生效的 mode：显式 override > 全局开关 > 规则自身声明。"""
    if override:
        return override if override in SUPPORTED_MODES else "warn"
    return gate_mode(rule_mode)


def available_codes(directory: Optional[Path] = None) -> list[str]:
    """扫描目录里实际存在的闸门 code（新增 E2/E3 时不必改代码）。"""
    directory = directory or rule_dir()
    if not directory.exists():
        return []
    codes = []
    for path in sorted(directory.glob("E*-*.yaml")):
        code = path.name.split("-", 1)[0]
        if code not in codes:
            codes.append(code)
    return codes


def load_rule(code: str, directory: Optional[Path] = None) -> GateRule:
    """按 code 读取并校验规则（E1 / E2 / E3）。"""
    directory = directory or rule_dir()
    path = directory / f"{code}-*.yaml"
    matches = sorted(directory.glob(f"{code}-*.yaml")) if directory.exists() else []
    if not matches:
        raise GateRuleError(f"未找到闸门规则：{path}（目录 {directory}）")
    raw = yaml.safe_load(matches[0].read_text(encoding="utf-8")) or {}
    return parse_rule(raw, str(matches[0]))


def parse_rule(raw: dict, source_path: str = "<inline>") -> GateRule:
    name = Path(source_path).name
    _require(isinstance(raw, dict), name, "-", "规则文件必须是对象")
    code = str(raw.get("code") or "")
    _require(bool(code), name, "-", "缺少 code")
    mode = str(raw.get("mode") or "warn").lower()
    _require(mode in SUPPORTED_MODES, name, code, f"mode 必须是 {SUPPORTED_MODES}，收到 {mode!r}")
    raw_checks = raw.get("checks") or []
    _require(isinstance(raw_checks, list) and raw_checks, name, code, "至少需要 1 条 check")
    checks: list[Check] = []
    seen: set[str] = set()
    for item in raw_checks:
        _require(isinstance(item, dict), name, code, "check 必须是对象")
        cid = str(item.get("id") or "")
        _require(bool(cid), name, code, "check 缺少 id")
        _require(cid not in seen, name, code, f"check id 重复：{cid}")
        seen.add(cid)
        require = str(item.get("require") or "non_empty")
        _require(require in SUPPORTED_REQUIRE, name, code, f"{cid}: require 必须是 {SUPPORTED_REQUIRE}，收到 {require!r}")
        severity = str(item.get("severity") or "major")
        _require(severity in SEVERITIES, name, code, f"{cid}: severity 必须是 {SEVERITIES}，收到 {severity!r}")
        keys = item.get("keys") or []
        _require(isinstance(keys, list), name, code, f"{cid}: keys 必须是列表")
        checks.append(Check(id=cid, ask=str(item.get("ask") or cid), require=require,
                            keys=[str(k) for k in keys], severity=severity,
                            hint=str(item.get("hint") or "")))
    return GateRule(
        code=code,
        title=str(raw.get("title") or code),
        mode=mode,
        checks=checks,
        version=str(raw.get("version") or ""),
        description=str(raw.get("description") or ""),
        source_path=source_path,
    )


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _resolve(context: dict, keys: list[str]) -> Any:
    """按 keys 取值：支持嵌套（a.b）与并列（任意一个非空即可）。"""
    for key in keys:
        node: Any = context
        ok = True
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok and not _is_blank(node):
            return node
    return None


def evaluate_check(check: Check, context: dict) -> dict:
    """判定单条检查项，返回 {id, ask, severity, passed, detail, value_preview}。"""
    keys = check.keys or [check.id]
    result = {"id": check.id, "ask": check.ask, "severity": check.severity, "require": check.require}

    if check.require in ("non_empty", "any_of", "all_of"):
        values = [_resolve(context, [k]) for k in keys]
        if check.require == "all_of":
            missing = [k for k, v in zip(keys, values) if _is_blank(v)]
            passed = not missing
            detail = "全部满足" if passed else f"缺少：{', '.join(missing)}"
        else:
            passed = any(not _is_blank(v) for v in values)
            detail = "已提供" if passed else f"未提供（需要：{', '.join(keys)}）"
        preview = next((v for v in values if not _is_blank(v)), None)
    elif check.require == "source_present":
        sources = context.get("sources") or []
        passed = bool(sources)
        detail = f"来源 {len(sources)} 条" if passed else "来源链为空：需求没有可回溯的出处"
        preview = [s.get("ref") for s in sources][:3] if passed else None
    elif check.require == "evidence_ids_present":
        evidence = context.get("evidence") or context.get("evidence_ids") or []
        passed = bool(evidence)
        detail = f"依据 {len(evidence)} 条" if passed else "缺少依据链接"
        preview = evidence[:3] if passed else None
    else:  # pragma: no cover - 由 load 阶段拦截
        passed, detail, preview = False, f"未知 require：{check.require}", None

    result.update({"passed": passed, "detail": detail, "value_preview": preview})
    if not passed and check.hint:
        result["hint"] = check.hint
    return result


def evaluate(rule: GateRule, context: dict, mode_override: Optional[str] = None) -> dict:
    """执行闸门判定。

    返回：`{code, mode, passed, blocked, gaps[], results[], rule_version, fingerprint, critical_missing}`

    - `passed`：所有检查项都过；
    - `blocked`：**实际是否应阻断推进**（mode=block 且（存在 critical 缺口 或 任何缺口））；
    - `mode`：`mode_override` > 规则内 mode > 全局 ENGINEERING_GATES_MODE > warn。
    """
    mode = resolve_mode(rule.mode, mode_override)
    results = [evaluate_check(c, context) for c in rule.checks]
    gaps = [r for r in results if not r["passed"]]
    critical_missing = [g["id"] for g in gaps if g["severity"] == "critical"]
    passed = not gaps
    blocked = bool(gaps) and mode == "block"
    return {
        "code": rule.code,
        "title": rule.title,
        "mode": mode,
        "passed": passed,
        "blocked": blocked,
        "gaps": gaps,
        "results": results,
        "critical_missing": critical_missing,
        "rule_version": rule.version,
        "fingerprint": rule.fingerprint(),
        "source_path": Path(rule.source_path).name,
    }


def render_gaps_text(result: dict) -> str:
    """给人/给模型看的缺口清单（阻断时必须能直接告诉对方差什么）。"""
    if result["passed"]:
        return f"{result['code']} {result['title']}：全部 {len(result['results'])} 项通过"
    tail = "已阻断推进" if result["blocked"] else f"mode={result['mode']}，仅记录"
    lines = [f"{result['code']} {result['title']}：{len(result['gaps'])} 项未通过（{tail}）"]
    for gap in result["gaps"]:
        mark = "⛔" if gap["severity"] == "critical" else "•"
        lines.append(f"  {mark} {gap['ask']} → {gap['detail']}")
        if gap.get("hint"):
            lines.append(f"     建议：{gap['hint']}")
    if result["critical_missing"]:
        lines.append(f"  关键项缺失：{', '.join(result['critical_missing'])}（关键未知不能被多数通过抵消）")
    return "\n".join(lines)


def result_to_dict(result: dict) -> dict:
    """转成可 JSON 序列化的形态（供 API/留痕）。"""
    return {k: v for k, v in asdict_free(result).items()}


def asdict_free(obj: Any) -> Any:  # pragma: no cover - 简单递归
    if isinstance(obj, dict):
        return {k: asdict_free(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [asdict_free(v) for v in obj]
    return obj


def describe_rule(rule: GateRule) -> dict:
    return {
        "code": rule.code,
        "title": rule.title,
        "mode": rule.mode,
        "version": rule.version,
        "fingerprint": rule.fingerprint(),
        "checks": [asdict(c) for c in rule.checks],
        "source": Path(rule.source_path).name,
    }
