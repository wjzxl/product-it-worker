"""WP1 模糊度检测与明示默认值测试。

验收口径（计划 WP1）：给定 12 条样例，≥10 条命中；并覆盖
出处挂接、默认值模板、渲染、以及"清晰需求不应误报"。
"""
from app.ambiguity import (
    DEFAULT_TEMPLATE,
    RULES,
    attach_sources,
    clarification_checklist,
    detect,
    explicit_defaults,
    render_clarification_text,
    summarize,
)

# 12 条模糊样例（对应课程图 17 的"看起来像需求、实际没限定实现"）
AMBIGUOUS_SAMPLES = [
    "列表加个导出，支持常用格式就行",
    "导出的主要字段跟列表页保持一致",
    "权限按常规处理，运营也能看",
    "数据量大的时候优化一下，必要时转后台",
    "范围你们定，我没意见",
    "报表尽快出来，尽量别太慢",
    "根据权限不同角色看到的内容不一样",
    "导出当前页还是选中行？都行，你们定",
    "这个统计要实时的，每月也要有",
    "正常情况没问题，异常情况你看着办",
    "支持批量操作，多个一起处理",
    "这次上线变更要能回滚吗？先上吧",
]

# 4 条清晰样例（不应误报）
CLEAR_SAMPLES = [
    "点击列表右上角「导出」，导出当前筛选条件下的全部结果；超过 5000 行转后台生成并邮件通知",
    "具备 export:user 权限的用户才可导出；运营角色不输出「成本」列",
    "空值导出为空字符串；时间为 UTC+8；金额保留两位小数",
    "若结果为空则展示空态文案；请求失败时给出可重试的错误提示",
]


class TestDetection:
    def test_hit_rate_at_least_10_of_12(self):
        hits = [s for s in AMBIGUOUS_SAMPLES if detect(s)]
        assert len(hits) >= 10, f"仅命中 {len(hits)}/12：{[s for s in AMBIGUOUS_SAMPLES if not detect(s)]}"

    def test_clear_samples_not_flagged(self):
        """清晰需求**一条都不该误报**——误报会让人不再信任闸门（对应计划 §8 误拦率）。"""
        false_positives = {s: [f.rule_id for f in detect(s)] for s in CLEAR_SAMPLES if detect(s)}
        assert not false_positives, f"误报样例：{false_positives}"

    def test_each_finding_carries_why_and_question(self):
        for f in detect("支持常用格式，主要字段等，数据量大时优化"):
            assert f.why and f.question, f"{f.rule_id} 缺少 why/question"
            assert f.suggested_defaults, f"{f.rule_id} 缺少建议默认值"

    def test_same_rule_reported_once(self):
        findings = detect("常用格式，还是常用格式")
        assert [f.rule_id for f in findings].count("format_unspecified") == 1

    def test_rule_ids_unique(self):
        ids = [r[0] for r in RULES]
        assert len(ids) == len(set(ids))

    def test_every_rule_has_unless_suppressor_and_compiles(self):
        """每条规则都必须能被抑制（避免"已写清也报"的误报），且正则合法。"""
        import re

        for rule_id, pattern, unless, why, question, defaults in RULES:
            assert unless, f"{rule_id} 缺少 unless 抑制条件"
            re.compile(pattern), re.compile(unless)


class TestSources:
    def test_attach_source_by_quote_match(self):
        findings = detect("导出支持常用格式就行")
        sources = [
            {"kind": "chat", "ref": "feishu://chat/oc_1/msg/om_1", "quote": "导出支持常用格式就行",
             "locator": {"msg_id": "om_1"}, "confirmed_by": "李经理"},
        ]
        attached = attach_sources(findings, sources)
        assert attached[0].source_ref["ref"] == "feishu://chat/oc_1/msg/om_1"
        assert attached[0].source_ref["locator"] == {"msg_id": "om_1"}

    def test_attach_falls_back_to_first_text_source(self):
        findings = detect("权限按常规处理")
        sources = [{"kind": "voice", "ref": "v1", "quote": "语音里没提权限"},
                   {"kind": "chat", "ref": "c1", "quote": "聊天原文"}]
        attached = attach_sources(findings, sources)
        # 文本类出处优先（voice 不是文本类）
        assert attached[0].source_ref["ref"] == "c1"

    def test_no_sources_is_tolerated(self):
        assert attach_sources(detect("常用格式"), None)[0].source_ref is None


class TestDefaults:
    def test_template_covers_course_required_items(self):
        keys = {item["key"] for item in DEFAULT_TEMPLATE}
        assert {"null_policy", "timezone", "number_precision", "batch_limit"} <= keys

    def test_explicit_defaults_filter(self):
        picked = explicit_defaults(["timezone"])
        assert len(picked) == 1 and picked[0]["default"].startswith("UTC+8")

    def test_every_default_has_question_and_default(self):
        assert all(item["question"] and item["default"] for item in DEFAULT_TEMPLATE)


class TestSummary:
    def test_summarize_marks_needs_clarification(self):
        result = summarize("列表加个导出，支持常用格式就行", sources=[
            {"kind": "chat", "ref": "feishu://chat/oc_1/msg/om_9", "quote": "支持常用格式就行"}
        ])
        assert result["needs_clarification"] is True
        assert result["ambiguity_count"] >= 1
        assert result["questions"][0]["source_ref"]["ref"].endswith("om_9")
        assert result["defaults_to_confirm"]

    def test_summarize_clean_requirement(self):
        result = summarize(CLEAR_SAMPLES[0] + "；权限点 export:user")
        assert result["needs_clarification"] is False

    def test_render_contains_questions_and_defaults(self):
        text = render_clarification_text(summarize("导出支持常用格式"))
        assert "需确认" in text or "需要确认" in text
        assert "建议默认值" in text and "UTC+8" in text

    def test_checklist_ids_stable(self):
        findings = detect("常用格式，数据量大")
        checklist = clarification_checklist(findings)
        assert [c["id"] for c in checklist] == [f"q-{i+1}" for i in range(len(findings))]
