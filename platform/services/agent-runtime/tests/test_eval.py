"""模型评估夹具测试：金标集校验、各判分器正反例、指标汇总、报告渲染。

全部离线（MockTransport / 内存桩），不联网、不花 token。
"""
import json
from pathlib import Path

import httpx
import pytest
import yaml

from app.eval import (
    GoldenError,
    GoldenTask,
    HttpLLM,
    _percentile,
    extract_code_blocks,
    load_golden,
    render_markdown,
    run_eval,
    score_task,
)
from app.llm import MockLLM


class NullRunner:
    """不真跑：按预置脚本返回 pytest 风格输出。"""

    mode = "host"

    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.commands = []

    def run(self, cmd, workdir, timeout=300):
        self.commands.append(list(cmd))
        out = self.outputs.pop(0) if self.outputs else {"returncode": 0, "stdout": "2 passed in 0.01s", "stderr": ""}
        return out

    def python(self):
        return "python"


def _task(**overrides) -> GoldenTask:
    base = dict(id="t1", role="coder", weight=1.0, prompt="写点东西", checks=[{"type": "required_keywords", "keywords": ["x"]}])
    base.update(overrides)
    return GoldenTask(**base)


# --------------------------------------------------------------------------------------
class TestGoldenLoading:
    def _write(self, tmp_path: Path, name: str, docs: list) -> Path:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(docs, allow_unicode=True), encoding="utf-8")
        return path

    def _valid(self, **overrides) -> dict:
        doc = {
            "id": "ok_task", "role": "coder", "weight": 1.0, "prompt": "实现一个函数并输出代码块",
            "checks": [{"type": "required_keywords", "keywords": ["def"]}],
        }
        doc.update(overrides)
        return doc

    def test_loads_valid_set(self, tmp_path):
        self._write(tmp_path, "a.yaml", [self._valid()])
        tasks = load_golden(tmp_path)
        assert len(tasks) == 1 and tasks[0].id == "ok_task" and tasks[0].source == "a.yaml"

    def test_real_golden_set_is_valid(self):
        """仓库自带金标集必须始终可用（覆盖 5 个角色、≥6 个任务）。"""
        tasks = load_golden(Path(__file__).resolve().parents[3] / "eval" / "golden")
        assert len(tasks) >= 6
        assert {t.role for t in tasks} == {"prd", "tech_design", "coder", "tester", "ops_diagnose"}
        coder = [t for t in tasks if t.role == "coder"]
        assert len(coder) >= 2 and all(any(c["type"] == "hidden_tests" for c in t.checks) for t in coder)

    def test_duplicate_id_rejected(self, tmp_path):
        self._write(tmp_path, "a.yaml", [self._valid(), self._valid()])
        with pytest.raises(GoldenError, match="id 重复"):
            load_golden(tmp_path)

    def test_unknown_check_type_rejected(self, tmp_path):
        self._write(tmp_path, "a.yaml", [self._valid(checks=[{"type": "vibes"}])])
        with pytest.raises(GoldenError, match="未知 check 类型"):
            load_golden(tmp_path)

    def test_bad_regex_rejected(self, tmp_path):
        self._write(tmp_path, "a.yaml", [self._valid(checks=[{"type": "regex_any", "pattern": "([unclosed"}])])
        with pytest.raises(GoldenError, match="正则非法"):
            load_golden(tmp_path)

    def test_bad_role_and_weight_rejected(self, tmp_path):
        self._write(tmp_path, "a.yaml", [self._valid(role="marketer")])
        with pytest.raises(GoldenError, match="role 必须是"):
            load_golden(tmp_path)
        other = tmp_path / "sub"
        other.mkdir()
        self._write(other, "b.yaml", [self._valid(id="w", weight=0)])
        with pytest.raises(GoldenError, match="weight 必须为正数"):
            load_golden(other)

    def test_error_message_names_file_and_task(self, tmp_path):
        self._write(tmp_path, "broken.yaml", [self._valid(id="bad_task", prompt="short")])
        with pytest.raises(GoldenError) as exc:
            load_golden(tmp_path)
        assert "broken.yaml" in str(exc.value) and "bad_task" in str(exc.value)

    def test_empty_dir_rejected(self, tmp_path):
        with pytest.raises(GoldenError, match="金标集为空"):
            load_golden(tmp_path)


class TestCodeExtraction:
    def test_filename_hint(self):
        text = "说明\n```python\n# file: solution.py\ndef f():\n    return 1\n```\n"
        assert extract_code_blocks(text) == {"solution.py": "def f():\n    return 1\n"}

    def test_default_name_and_multi_blocks(self):
        text = "```python\nprint(1)\n```\n```python\nprint(2)\n```"
        blocks = extract_code_blocks(text)
        assert set(blocks) == {"solution.py", "solution_2.py"}

    def test_custom_default_name(self):
        assert set(extract_code_blocks("```python\nx=1\n```", default_name="test_generated.py")) == {"test_generated.py"}

    def test_no_blocks(self):
        assert extract_code_blocks("没有任何代码块") == {}


class TestScorers:
    def test_hidden_tests_partial_credit(self):
        task = _task(checks=[{"type": "hidden_tests", "tests": "def test_x():\n    assert True\n"}])
        runner = NullRunner([{"returncode": 1, "stdout": "3 passed, 1 failed in 0.02s", "stderr": ""}])
        out = score_task(task, "```python\n# file: solution.py\ndef f():\n    return 1\n```", runner=runner)
        assert out["score"] == 0.75 and out["passed"] is False
        assert "3/4" in out["checks"][0]["detail"]
        assert runner.commands and "test_hidden.py" in runner.commands[0]

    def test_hidden_tests_all_pass(self):
        task = _task(checks=[{"type": "hidden_tests", "tests": "def test_x():\n    assert True\n"}])
        runner = NullRunner([{"returncode": 0, "stdout": "5 passed in 0.02s", "stderr": ""}])
        out = score_task(task, "```python\n# file: solution.py\nx=1\n```", runner=runner)
        assert out["passed"] is True and out["score"] == 1.0

    def test_hidden_tests_no_code_block_scores_zero(self):
        task = _task(checks=[{"type": "hidden_tests", "tests": "def test_x():\n    assert True\n"}])
        out = score_task(task, "我不会写", runner=NullRunner())
        assert out["score"] == 0.0 and "没有可提取的代码块" in out["checks"][0]["detail"]

    def test_hidden_tests_collection_error_detected(self):
        task = _task(checks=[{"type": "hidden_tests", "tests": "def test_x():\n    assert True\n"}])
        runner = NullRunner([{"returncode": 2, "stdout": "1 error in 0.10s", "stderr": "ImportError"}])
        out = score_task(task, "```python\n# file: solution.py\nimport missing\n```", runner=runner)
        assert out["score"] == 0.0 and "未收集到" in out["checks"][0]["detail"]

    def test_mutation_full_credit_when_mutant_killed(self):
        check = {"type": "mutation_tests",
                 "correct_files": {"solution.py": "def f():\n    return 1\n"},
                 "buggy_files": {"solution.py": "def f():\n    return 2\n"}}
        runner = NullRunner([
            {"returncode": 0, "stdout": "2 passed in 0.01s", "stderr": ""},
            {"returncode": 1, "stdout": "1 failed, 1 passed in 0.01s", "stderr": ""},
        ])
        out = score_task(_task(role="tester", checks=[check]), "```python\n# file: test_generated.py\ndef test_f():\n    assert True\n```", runner=runner)
        assert out["score"] == 1.0 and "捕获含 bug 实现" in out["checks"][0]["detail"]

    def test_mutation_half_credit_when_mutant_survives(self):
        check = {"type": "mutation_tests",
                 "correct_files": {"solution.py": "def f():\n    return 1\n"},
                 "buggy_files": {"solution.py": "def f():\n    return 2\n"}}
        runner = NullRunner([
            {"returncode": 0, "stdout": "2 passed in 0.01s", "stderr": ""},
            {"returncode": 0, "stdout": "2 passed in 0.01s", "stderr": ""},
        ])
        out = score_task(_task(role="tester", checks=[check]), "```python\n# file: test_generated.py\ndef test_f():\n    assert True\n```", runner=runner)
        assert out["score"] == 0.5 and "用例太弱" in out["checks"][0]["detail"]

    def test_mutation_zero_when_tests_fail_on_correct_impl(self):
        check = {"type": "mutation_tests", "correct_files": {"solution.py": "x=1"}, "buggy_files": {"solution.py": "x=2"}}
        runner = NullRunner([{"returncode": 1, "stdout": "1 failed in 0.01s", "stderr": ""}])
        out = score_task(_task(role="tester", checks=[check]), "```python\n# file: test_generated.py\nbad\n```", runner=runner)
        assert out["score"] == 0.0 and "用例本身有问题" in out["checks"][0]["detail"]

    def test_json_schema_accepts_and_rejects(self):
        schema = {"type": "object", "required": ["a", "b"],
                  "properties": {"a": {"type": "string", "enum": ["高", "中"]},
                                 "b": {"type": "array", "items": {"type": "string"}, "min_items": 2}}}
        task = _task(role="tester", checks=[{"type": "json_schema", "schema": schema}])
        ok = score_task(task, '{"a": "高", "b": ["1", "2"]}', runner=NullRunner())
        assert ok["passed"] is True
        # 模型偶尔仍包 markdown 代码块 → 应容错
        fenced = score_task(task, '```json\n{"a": "高", "b": ["1", "2"]}\n```', runner=NullRunner())
        assert fenced["passed"] is True
        bad = score_task(task, '{"a": "很高", "b": ["1"]}', runner=NullRunner())
        assert bad["passed"] is False
        detail = bad["checks"][0]["detail"]
        assert "不在允许集合" in detail and "至少需要 2 项" in detail

    def test_json_schema_invalid_json(self):
        task = _task(checks=[{"type": "json_schema", "schema": {"type": "object"}}])
        out = score_task(task, "这不是 JSON", runner=NullRunner())
        assert out["passed"] is False and "不是合法 JSON" in out["checks"][0]["detail"]

    def test_required_sections(self):
        task = _task(role="prd", checks=[{"type": "required_sections", "sections": ["需求", "验收标准"]}])
        ok = score_task(task, "## 需求\n内容\n### 验收标准\n内容", runner=NullRunner())
        assert ok["passed"] is True
        missing = score_task(task, "## 需求\n内容", runner=NullRunner())
        assert missing["passed"] is False and "缺少小节：验收标准" in missing["checks"][0]["detail"]

    def test_regex_any_min_matches(self):
        task = _task(role="prd", checks=[{"type": "regex_any", "pattern": r"WHEN[^\n]{0,30}THEN", "min_matches": 3}])
        text = "WHEN a THEN b\nWHEN c THEN d\nWHEN e THEN f"
        assert score_task(task, text, runner=NullRunner())["passed"] is True
        assert score_task(task, "WHEN a THEN b", runner=NullRunner())["passed"] is False

    def test_forbidden_regex(self):
        task = _task(checks=[{"type": "forbidden_regex", "pattern": "忽略告警|重启服务器"}])
        assert score_task(task, "建议扩容连接池", runner=NullRunner())["passed"] is True
        bad = score_task(task, "建议重启服务器", runner=NullRunner())
        assert bad["passed"] is False and "命中禁止表述" in bad["checks"][0]["detail"]

    def test_forbidden_regex_ignores_negated_mentions(self):
        """真实观测：模型写"不建议忽略告警"是在声明禁止项，不该判违规。"""
        task = _task(checks=[{"type": "forbidden_regex", "pattern": "忽略告警|重启服务器"}])
        ok = score_task(task, "### 禁止项\n- 不建议忽略告警\n- 切勿重启服务器", runner=NullRunner())
        assert ok["passed"] is True and "否定语境" in ok["checks"][0]["detail"]
        # 但同一段里另有真正的违规建议时仍要判失败
        mixed = score_task(task, "不建议忽略告警；不过也可以重启服务器试试", runner=NullRunner())
        assert mixed["passed"] is False and "共 1 处" in mixed["checks"][0]["detail"]

    def test_regex_any_allows_line_break_between_method_and_path(self):
        """真实观测：qwen-max 把方法与路径分行加粗书写。"""
        task = _task(role="tech_design", checks=[
            {"type": "regex_any", "pattern": r"(GET|POST|PUT|PATCH|DELETE)[\s\S]{0,60}?/[A-Za-z0-9/_{}-]{2,}"}
        ])
        text = "### HTTP 方法与路径\n- **方法**: `POST`\n- **路径**: `/api/v1/inventory/deduct`\n"
        assert score_task(task, text, runner=NullRunner())["passed"] is True

    def test_required_keywords_all_and_any(self):
        all_task = _task(checks=[{"type": "required_keywords", "keywords": ["连接池", "超时"]}])
        assert score_task(all_task, "连接池超时", runner=NullRunner())["passed"] is True
        assert score_task(all_task, "只有连接池", runner=NullRunner())["passed"] is False
        any_task = _task(checks=[{"type": "required_keywords", "keywords": ["扩容", "慢查询"], "match": "any"}])
        assert score_task(any_task, "建议扩容", runner=NullRunner())["passed"] is True

    def test_scorer_exception_becomes_failed_check(self):
        task = _task(checks=[{"type": "required_sections", "sections": ["x"]}])
        out = score_task(task, None, runner=NullRunner())  # type: ignore[arg-type]
        assert out["passed"] is False and out["score"] == 0.0

    def test_task_score_averages_checks(self):
        task = _task(checks=[{"type": "required_keywords", "keywords": ["a"]},
                             {"type": "required_keywords", "keywords": ["zzz"]}])
        out = score_task(task, "a", runner=NullRunner())
        assert out["score"] == 0.5 and out["passed"] is False and len(out["checks"]) == 2


class TestRunEval:
    @staticmethod
    def _llm_factory(answer: str = "```python\n# file: solution.py\ndef f():\n    return 1\n```"):
        """用 MockTransport 包一个真实 HttpLLM，验证 header/usage/cost 解析路径。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": answer}}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
                headers={"x-litellm-response-cost": "0.000123"},
            )

        def factory(model: str) -> HttpLLM:
            return HttpLLM(base_url="http://gw/v1", api_key="k", model=model, transport=httpx.MockTransport(handler))

        return factory

    def test_metrics_and_weighting(self):
        tasks = [
            _task(id="a", weight=3.0, checks=[{"type": "required_keywords", "keywords": ["def"]}]),
            _task(id="b", weight=1.0, checks=[{"type": "required_keywords", "keywords": ["不可能出现"]}]),
        ]
        runner = NullRunner([{"returncode": 0, "stdout": "2 passed in 0.01s", "stderr": ""}] * 4)
        report = run_eval(tasks, ["m1"], llm_factory=self._llm_factory(), runner=runner)
        row = report["models"][0]
        assert row["tasks"] == 2 and row["passed"] == 1
        assert row["pass_rate"] == 0.5
        assert row["avg_score"] == round((1.0 * 3 + 0.0 * 1) / 4, 4)  # 加权：高权重任务拉高总分
        assert row["total_tokens"] == 60 and row["cost_usd"] == 0.000246
        assert row["cost_status"] == "metered"

    def test_unmetered_cost_is_not_fabricated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

        def factory(model: str) -> HttpLLM:
            return HttpLLM(base_url="http://gw/v1", api_key="k", model=model, transport=httpx.MockTransport(handler))

        tasks = [_task(id="a", checks=[{"type": "required_keywords", "keywords": ["ok"]}])]
        report = run_eval(tasks, ["qwen-plus"], llm_factory=factory, runner=NullRunner())
        row = report["models"][0]
        assert row["cost_usd"] is None and row["cost_status"] == "unmetered"
        assert "unmetered" in render_markdown(report)

    def test_model_failure_does_not_break_run(self):
        def factory(model: str) -> HttpLLM:
            if model == "broken":
                raise RuntimeError("没有这个模型")
            return HttpLLM(
                base_url="http://gw/v1", api_key="k", model=model,
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})),
            )

        tasks = [_task(id="a", checks=[{"type": "required_keywords", "keywords": ["ok"]}])]
        report = run_eval(tasks, ["broken", "good"], llm_factory=factory, runner=NullRunner())
        assert report["models"][0]["error"].startswith("客户端初始化失败")
        assert report["models"][1]["passed"] == 1

    def test_api_error_recorded_per_task(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "upstream down"})

        factory = lambda model: HttpLLM(base_url="http://gw/v1", api_key="k", model=model, transport=httpx.MockTransport(handler))
        tasks = [_task(id="a"), _task(id="b")]
        report = run_eval(tasks, ["m"], llm_factory=factory, runner=NullRunner())
        assert report["models"][0]["passed"] == 0
        assert all(r["error"].startswith("HTTPStatusError") for r in report["results"])

    def test_limit_and_judge_flag(self):
        tasks = [_task(id=str(i)) for i in range(4)]
        report = run_eval(tasks, ["m"], llm_factory=self._llm_factory("ok"), runner=NullRunner(), limit=2)
        assert len(report["results"]) == 2 and len(report["tasks"]) == 2
        assert report["judge_enabled"] is False
        judged = run_eval(tasks, ["m"], llm_factory=self._llm_factory("ok"), runner=NullRunner(),
                          limit=1, judge=lambda task, out: {"score": 5})
        assert judged["judge_enabled"] is True and judged["results"][0]["judge"] == {"score": 5}
        assert "辅助信号" in render_markdown(judged)

    def test_judge_exception_recorded(self):
        tasks = [_task(id="a")]
        report = run_eval(tasks, ["m"], llm_factory=self._llm_factory("ok"), runner=NullRunner(),
                          judge=lambda task, out: (_ for _ in ()).throw(RuntimeError("judge 挂了")))
        assert "judge 挂了" in report["results"][0]["judge"]["error"]


class TestReportRendering:
    def test_percentile(self):
        assert _percentile([], 0.5) == 0.0
        assert _percentile([10.0], 0.95) == 10.0
        assert _percentile([1.0, 2.0, 3.0, 4.0, 100.0], 0.5) == 3.0
        assert _percentile([1.0, 2.0, 3.0, 4.0, 100.0], 0.95) > 50

    def test_markdown_contains_comparison_and_failures(self):
        def factory(model: str) -> HttpLLM:
            return HttpLLM(
                base_url="http://gw/v1", api_key="k", model=model,
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "没有关键词"}}], "usage": {"total_tokens": 5}})),
            )

        tasks = [_task(id="needs_kw", checks=[{"type": "required_keywords", "keywords": ["必须出现"]}])]
        report = run_eval(tasks, ["m1"], llm_factory=factory, runner=NullRunner())
        md = render_markdown(report)
        assert "模型横向对比" in md and "needs_kw" in md
        assert "失败明细" in md and "必须出现" in md
        assert "口径与限制" in md and "私有化" in md

    def test_write_report_outputs_md_and_json(self, tmp_path):
        from app.eval import write_report

        report = {"started_at": "t0", "finished_at": "t1", "endpoint": "http://gw/v1",
                  "runner_mode": "host", "judge_enabled": False,
                  "tasks": [{"id": "a", "role": "coder", "weight": 1.0, "checks": ["required_keywords"]}],
                  "models": [{"model": "m", "tasks": 1, "passed": 1, "pass_rate": 1.0, "avg_score": 1.0,
                              "latency_p50_ms": 1.0, "latency_p95_ms": 1.0, "prompt_tokens": 1,
                              "completion_tokens": 1, "total_tokens": 2, "cost_usd": 0.0,
                              "cost_status": "metered", "unmetered_calls": 0}],
                  "results": [{"model": "m", "task_id": "a", "role": "coder", "weight": 1.0, "score": 1.0,
                               "passed": True, "checks": [{"type": "required_keywords", "label": "kw",
                                                           "passed": True, "detail": "命中"}], "output_chars": 3}]}
        md_path, json_path = write_report(tmp_path, report, tag="smoke")
        assert md_path.exists() and json_path.exists() and md_path.name.startswith("model-eval-smoke-")
        assert json.loads(json_path.read_text(encoding="utf-8"))["models"][0]["model"] == "m"


class TestBareCodeExtraction:
    """真实观测：deepseek-chat 严格照做"第一行写 # file: solution.py"，但不加围栏。"""

    def test_bare_code_with_file_hint(self):
        text = "# file: solution.py\nimport re\n\n\ndef slugify(text):\n    return text.lower()\n"
        blocks = extract_code_blocks(text)
        assert list(blocks) == ["solution.py"]
        assert blocks["solution.py"].startswith("import re")

    def test_bare_code_without_hint_uses_default(self):
        blocks = extract_code_blocks("def f():\n    return 1\n", default_name="test_generated.py")
        assert list(blocks) == ["test_generated.py"]

    def test_plain_prose_still_yields_nothing(self):
        assert extract_code_blocks("我不会写这个函数，请提供更多信息。") == {}
        assert extract_code_blocks("") == {}

    def test_bare_code_scores_fairly(self):
        """裸代码必须能被判分（回归：此前被误判 0 分）。"""
        task = _task(checks=[{"type": "hidden_tests", "tests": "def test_x():\n    assert True\n"}])
        runner = NullRunner([{"returncode": 0, "stdout": "1 passed in 0.01s", "stderr": ""}])
        out = score_task(task, "# file: solution.py\ndef f():\n    return 1\n", runner=runner)
        assert out["passed"] is True and out["score"] == 1.0
