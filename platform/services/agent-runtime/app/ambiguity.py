"""WP1 模糊度检测与"明示默认值"模板。

对应《从 vibe coding 到 FDE 交付》图 17 / 图 19：
  - 图 17：「常用格式」「主要字段」「按常规处理」「数据量大时优化」看起来像需求，
    实际把关键决定推给下游实现者；
  - 图 19：判断标准是「还要不要猜」——给两个不同的人或两个新会话，会不会做出明显不同的东西？

所以本模块只做一件事：**把"还要猜"的地方找出来，变成可确认的问题**，并附上建议的明示默认值
（图 15：业务方说"其他你们定"时，不能当无条件授权，要提出默认值再取得确认）。

设计取舍：
- 规则（词表 + 正则）为主，**不依赖 LLM**——判定必须可单测、可复现、可在 CI 里跑；
- 每条命中都给出 `why`（为什么这是模糊的）与 `question`（该问什么），
  因为"知道要问什么"才是经验，而不是"检测到模糊词"；
- LLM 只用于可选复核（`review_with_llm`），且结论必须落到同一结构上。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

# 每条规则：(id, 触发正则, 已明确则抑制的正则, 为什么模糊, 该问什么, 建议默认值)
#
# `unless` 是**误报抑制**：同一句话里往往既出现触发词、又已经写清了答案
# （例如"导出当前筛选条件下的全部结果"含"筛选结果"，但范围其实已明确）。
# 误报会让人不再信任闸门，所以宁可少报：先看 unless，命中则不报。
RULES: list[tuple[str, str, str, str, str, list[str]]] = [
    (
        "format_unspecified",
        r"常用格式|常见格式|标准格式|各种格式|支持导出",
        r"CSV|XLSX|EXCEL|JSON|UTF-8|编码",
        "「常用格式」没有限定具体格式与编码，实现者只能自选",
        "导出/接口的具体格式与编码是什么？（CSV / XLSX / JSON；UTF-8 带 BOM 与否）",
        ["CSV（UTF-8 带 BOM，Excel 可直接打开）"],
    ),
    (
        "fields_unspecified",
        r"主要字段|关键字段|等字段|字段等|必要字段|相关字段",
        r"字段清单|字段列表如下|以下字段",
        "「主要字段等」没有穷尽字段集合，字段清单决定表结构与报表口径",
        "导出的完整字段清单是什么？是否包含成本等敏感列？",
        ["字段清单以现网列表页为准", "成本列仅财务角色可见"],
    ),
    (
        "convention_unspecified",
        r"按常规|常规处理|照旧|老规矩|跟以前一样|按惯例",
        r"常规指|具体为|处理方式如下",
        "「按常规处理」不是可验收标准：不同人对「常规」的实现不同",
        "「常规」具体指哪种处理？异常/空值/超限时分别怎么做？",
        ["空值导出为空字符串", "异常行跳过并在结果中标注行号"],
    ),
    (
        "threshold_unspecified",
        r"数据量大|大数据量|量大时|性能优化|必要时|视情况",
        r"\d+\s*行|阈值\s*\d+|\d+\s*条|P99|毫秒|ms\b",
        "缺少阈值：多大算大？超限后是转后台、拒绝还是降级？",
        "触发后台生成的行数阈值是多少？超限后如何通知？",
        ["超过 5000 行转后台生成，完成后站内信 + 邮件通知"],
    ),
    (
        "authorization_vague",
        r"其他你们定|你们定|随便|都行|你看着办|无所谓",
        r"默认值|确认如下|建议为",
        "把未决事项当成无条件授权，等于把业务决定留给实现者",
        "这些未定项请确认建议默认值（逐条列出后取得确认，而不是默认采纳）",
        ["逐条确认下方「明示默认值」清单"],
    ),
    (
        "degree_unspecified",
        r"尽快|尽量|酌情|适当|合理|友好|优化一下",
        r"P99|ms|毫秒|分钟内|秒内|SLO",
        "程度类表述无法判定通过与否（多快算尽快？）",
        "可量化的目标是什么？（时限 / 数值 / 可观测指标）",
        ["接口 P99 ≤ 500ms", "导出任务 5 分钟内完成"],
    ),
    (
        "permission_unspecified",
        r"权限控制|权限管理|根据权限|不同角色|该看到的",
        r"权限点|export:|角色不|仅.{0,6}可见",
        "没有指明角色与字段的对应关系，权限是审批与测试的判据",
        "哪些角色可以执行该操作、可以看到哪些字段？",
        ["具备 export:user 权限才可导出", "运营角色不输出成本列"],
    ),
    (
        "scope_unspecified",
        r"当前页|选中行|全部|筛选结果|部分数据",
        r"当前筛选条件下的全部|全部筛选结果|范围明确|确认为",
        "导出/处理范围未定：当前页、选中行还是全部筛选结果，实现完全不同",
        "范围是当前页、选中行，还是当前筛选条件下的全部结果？",
        ["导出当前筛选条件下的全部结果（非当前页）"],
    ),
    (
        "time_unspecified",
        r"近期|最近|实时|定时|每天|每月",
        r"UTC|时区|Asia/|自然月|每日\s*\d{1,2}[:：]",
        "时间口径未定：时区、起止边界、按天还是按月聚合，结果会不同",
        "时间范围与时区口径是什么？按天还是按月、是否分仓聚合？",
        ["时区 UTC+8", "按自然月 × 仓库聚合"],
    ),
    (
        "boundary_unspecified",
        r"正常情况|一般情况下|通常|多数场景",
        r"空态|空结果|错误提示|可重试|超时",
        "只描述顺路径，未定义空态/错误态/边界（课程图 31：完整性最容易漏的部分）",
        "空结果、请求失败、超时、无权限时分别怎么表现？",
        ["空结果展示空态文案", "失败给出可重试的错误提示"],
    ),
    (
        "quantity_unspecified",
        r"多个|若干|若干条|少量|大量|批量",
        r"最多\s*\d+|上限\s*\d+|不超过\s*\d+",
        "数量与批量上限未定，直接影响分页、限流与实现方式",
        "单次操作的数量上限是多少？超限如何处理？",
        ["单次最多 100 条，超出提示分批"],
    ),
    (
        "rollback_unspecified",
        r"上线|发布|变更|迁移|回滚",
        r"回滚方式|一键切回|保留上一版本|灰度|回滚验证",
        "变更类诉求未写回滚方式与验证口径，属工程闸门 E1 的必答项",
        "如何回滚？回滚后如何验证已恢复？",
        ["保留上一版本，可一键切回并按灰度指标验证"],
    ),
]

# 图 15：无论业务方是否说"其他你们定"，这些影响结果的默认值都必须显式确认
DEFAULT_TEMPLATE: list[dict] = [
    {"key": "null_policy", "question": "空值如何导出/展示？", "default": "空字符串（不写 NULL 字面量）"},
    {"key": "timezone", "question": "时间按哪个时区？", "default": "UTC+8（Asia/Shanghai）"},
    {"key": "number_precision", "question": "金额/比率精度？", "default": "金额两位小数，比率两位小数百分比"},
    {"key": "batch_limit", "question": "单次数据量上限与超限行为？", "default": "5000 行转后台生成并通知"},
    {"key": "permission_model", "question": "权限粒度？", "default": "按操作权限点（如 export:user）+ 字段级脱敏"},
    {"key": "audit_trail", "question": "是否留操作记录？", "default": "记录导出人、时间、条件、行数（可追溯）"},
]


@dataclass
class Finding:
    """一条模糊表述的检测结果。"""

    rule_id: str
    matched: str
    why: str
    question: str
    suggested_defaults: list[str] = field(default_factory=list)
    source_ref: Optional[dict] = None  # 出处（有材料时填充，见 attach_sources）

    def to_dict(self) -> dict:
        return asdict(self)


def detect(text: str) -> list[Finding]:
    """检测模糊表述。

    - 同一规则多次命中只报一次（保留首次命中片段）；
    - 命中 `unless`（说明该项其实已写清）则**不报**——宁可少报，避免误报侵蚀信任。
    """
    findings: list[Finding] = []
    text = text or ""
    for rule_id, pattern, unless, why, question, defaults in RULES:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        if unless and re.search(unless, text, re.IGNORECASE):
            continue
        findings.append(
            Finding(
                rule_id=rule_id,
                matched=m.group(0),
                why=why,
                question=question,
                suggested_defaults=list(defaults),
            )
        )
    return findings


def attach_sources(findings: list[Finding], sources: Optional[list[dict]] = None) -> list[Finding]:
    """给每条发现挂上出处（课程图 11：每条需求都带出处）。

    匹配策略：若某条出处的原文摘录里出现了同样的模糊表述，则引用它；
    否则回退到第一条文本类出处（说明该问题源自需求原文而非某份材料）。
    """
    sources = sources or []
    text_sources = [s for s in sources if s.get("kind") in ("chat", "issue", "doc", "mail", "manual")]
    for f in findings:
        hit = next((s for s in sources if f.matched and f.matched in (s.get("quote") or "")), None)
        ref = hit or (text_sources[0] if text_sources else None)
        if ref:
            f.source_ref = {
                "kind": ref.get("kind", "chat"),
                "ref": ref.get("ref", ""),
                "quote": ref.get("quote", "") or f.matched,
                "locator": ref.get("locator") or {},
                "confirmed_by": ref.get("confirmed_by", ""),
            }
    return findings


def clarification_checklist(findings: list[Finding]) -> list[dict]:
    """把发现转成可直接发给业务方的待确认清单。"""
    return [
        {
            "id": f"q-{i + 1}",
            "rule_id": f.rule_id,
            "matched": f.matched,
            "question": f.question,
            "why": f.why,
            "suggested_defaults": f.suggested_defaults,
            "source_ref": f.source_ref,
        }
        for i, f in enumerate(findings)
    ]


def explicit_defaults(keys: Optional[list[str]] = None) -> list[dict]:
    """明示默认值清单（图 15）：未指定则返回全部。"""
    if not keys:
        return [dict(item) for item in DEFAULT_TEMPLATE]
    return [dict(item) for item in DEFAULT_TEMPLATE if item["key"] in keys]


def summarize(text: str, sources: Optional[list[dict]] = None, extra_notes: str = "") -> dict:
    """澄清节点输出：模糊清单 + 待确认问题 + 明示默认值 + 来源。"""
    findings = attach_sources(detect(text), sources)
    checklist = clarification_checklist(findings)
    return {
        "requirement": text,
        "ambiguity_count": len(findings),
        "findings": [f.to_dict() for f in findings],
        "questions": checklist,
        "defaults_to_confirm": explicit_defaults(),
        "sources": sources or [],
        "extra_notes": extra_notes,
        "needs_clarification": bool(findings),
    }


def render_clarification_text(result: dict) -> str:
    """把 summarize 的结果渲染成给人看/给下游 prompt 用的文本。"""
    lines = [f"需求澄清结果：\n- 原始诉求：{result.get('requirement', '')}"]
    if result.get("needs_clarification"):
        lines.append(f"- 发现 {result['ambiguity_count']} 处需要确认的模糊表述：")
        for q in result["questions"]:
            src = q.get("source_ref") or {}
            where = f"（出处：{src.get('kind', '')} {src.get('ref', '')}）" if src else ""
            lines.append(f"  {q['id']}. 命中「{q['matched']}」→ {q['question']}{where}")
            if q["suggested_defaults"]:
                lines.append(f"     建议默认值：{'；'.join(q['suggested_defaults'])}")
    else:
        lines.append("- 未发现明显模糊表述（仍需人确认范围与权限）")
    lines.append("- 需逐条确认的明示默认值：")
    for item in result.get("defaults_to_confirm", []):
        lines.append(f"  · {item['question']} → 建议：{item['default']}")
    if result.get("extra_notes"):
        lines.append(f"- 备注：{result['extra_notes']}")
    return "\n".join(lines)
