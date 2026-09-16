"""多模型 / 私有化模型离线评估夹具（Phase 3 ③）。

目的：**换模型前先量出来**。平台的每一步 AI 产出（PRD、方案、代码、用例、诊断）都由
LLM 完成，选型不能靠感觉。本模块提供离线金标评测集 + 机器判分 + 报告，
支持把任意 OpenAI 兼容端点（LiteLLM 网关 / vLLM / Ollama / 私有化部署）拉进来横向对比。

判分原则（重要）：
- **机器判分优先**：代码类任务在沙箱里跑隐藏测试；结构化产出校验 JSON Schema；
  文档类校验必备小节与关键表述。LLM 评审（judge）**默认关闭**，开启时在报告里明确标注
  "辅助信号，不作唯一评分依据"。
- **不编造成本**：成本取自网关返回的 `x-litellm-response-cost` 头；拿不到就标 `unmetered`
  （例如自定义 DashScope 端点没有价格映射），绝不用估算值冒充真实花费。

用法（编程接口）：
    from app.eval import load_golden, run_eval, HttpLLM, write_report
    tasks = load_golden("platform/eval/golden")
    report = run_eval(tasks, ["deepseek-chat"], llm_factory=HttpLLM.from_env)
    write_report("platform/reports", report, tag="baseline")
"""
from __future__ import annotations

import json
import os
import re
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import yaml

from .sandbox import get_runner

__all__ = [
    "GoldenError",
    "GoldenTask",
    "HttpLLM",
    "load_golden",
    "run_eval",
    "score_task",
    "extract_code_blocks",
    "render_markdown",
    "write_report",
]

CHECK_TYPES = (
    "hidden_tests",
    "mutation_tests",
    "json_schema",
    "required_sections",
    "regex_any",
    "forbidden_regex",
    "required_keywords",
)
# severity 等枚举用；分数口径统一为 check 数加权，不引入主观权重
DEFAULT_TIMEOUT = 180
SANDBOX_TIMEOUT = 180
ROLES = ("prd", "tech_design", "coder", "tester", "ops_diagnose")


class GoldenError(ValueError):
    """金标集格式错误（报错需指明文件与任务）。"""


# --------------------------------------------------------------------------------------
# 金标集加载与校验
# --------------------------------------------------------------------------------------
@dataclass
class GoldenTask:
    id: str
    role: str
    weight: float
    prompt: str
    checks: list[dict]
    system: str = ""
    source: str = ""


def _require(cond: bool, source: str, task_id: str, msg: str) -> None:
    if not cond:
        raise GoldenError(f"{source} · 任务 `{task_id}`: {msg}")


def _validate_check(raw: dict, source: str, task_id: str) -> dict:
    _require(isinstance(raw, dict), source, task_id, "check 必须是对象")
    kind = raw.get("type")
    _require(kind in CHECK_TYPES, source, task_id, f"未知 check 类型 {kind!r}（可用：{', '.join(CHECK_TYPES)}）")
    if kind == "hidden_tests":
        _require(bool(raw.get("tests")), source, task_id, "hidden_tests 缺少 tests 源码")
    elif kind == "mutation_tests":
        _require(bool(raw.get("correct_files")), source, task_id, "mutation_tests 缺少 correct_files")
        _require(bool(raw.get("buggy_files")), source, task_id, "mutation_tests 缺少 buggy_files")
    elif kind == "json_schema":
        _require(isinstance(raw.get("schema"), dict), source, task_id, "json_schema 缺少 schema")
    elif kind == "required_sections":
        _require(bool(raw.get("sections")), source, task_id, "required_sections 缺少 sections")
    elif kind in ("regex_any", "forbidden_regex"):
        _require(bool(raw.get("pattern")), source, task_id, f"{kind} 缺少 pattern")
        try:
            re.compile(raw["pattern"])
        except re.error as exc:
            raise GoldenError(f"{source} · 任务 `{task_id}`: 正则非法 {raw['pattern']!r}（{exc}）") from exc
    elif kind == "required_keywords":
        _require(bool(raw.get("keywords")), source, task_id, "required_keywords 缺少 keywords")
        mode = raw.get("match", "all")
        _require(mode in ("all", "any"), source, task_id, f"required_keywords.match 只能是 all/any，收到 {mode!r}")
    return raw


def load_golden(path: str | Path) -> list[GoldenTask]:
    """加载并校验金标集（目录下所有 *.yaml/*.yml）。校验失败直接抛错，不做静默跳过。"""
    root = Path(path)
    files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")]) if root.is_dir() else [root]
    if not files:
        raise GoldenError(f"金标集为空：{root}")
    tasks: list[GoldenTask] = []
    seen: set[str] = set()
    for file in files:
        try:
            docs = yaml.safe_load(file.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as exc:
            raise GoldenError(f"{file.name}: YAML 解析失败（{exc}）") from exc
        _require(isinstance(docs, list), file.name, "-", "顶层必须是任务列表")
        for raw in docs:
            _require(isinstance(raw, dict), file.name, "-", "任务必须是对象")
            task_id = str(raw.get("id") or "")
            _require(bool(task_id), file.name, "-", "缺少 id")
            _require(task_id not in seen, file.name, task_id, "id 重复")
            seen.add(task_id)
            role = str(raw.get("role") or "")
            _require(role in ROLES, file.name, task_id, f"role 必须是 {ROLES} 之一，收到 {role!r}")
            prompt = str(raw.get("prompt") or "")
            _require(len(prompt) >= 10, file.name, task_id, "prompt 过短（<10 字符）")
            weight = float(raw.get("weight", 1.0))
            _require(weight > 0, file.name, task_id, f"weight 必须为正数，收到 {weight}")
            checks = raw.get("checks") or []
            _require(isinstance(checks, list) and checks, file.name, task_id, "至少需要 1 条 check")
            tasks.append(
                GoldenTask(
                    id=task_id,
                    role=role,
                    weight=weight,
                    prompt=prompt,
                    checks=[_validate_check(c, file.name, task_id) for c in checks],
                    system=str(raw.get("system") or ""),
                    source=file.name,
                )
            )
    return tasks


# --------------------------------------------------------------------------------------
# LLM 客户端（任意 OpenAI 兼容端点，含私有化部署）
# --------------------------------------------------------------------------------------
class HttpLLM:
    """OpenAI 兼容 chat 客户端：返回文本 + token 用量 + 网关计费（拿不到则 None）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
        temperature: float = 0.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self._client = httpx.Client(timeout=timeout, transport=transport)

    @classmethod
    def from_env(cls, model: str, **kwargs) -> "HttpLLM":
        """默认打 LiteLLM 网关；`EVAL_BASE_URL`/`EVAL_API_KEY` 可指向私有化端点。"""
        return cls(
            base_url=os.getenv("EVAL_BASE_URL", "http://127.0.0.1:4000/v1"),
            api_key=os.getenv("EVAL_API_KEY", os.getenv("LLM_API_KEY", "sk-litellm-master")),
            model=model,
            **kwargs,
        )

    def chat(self, messages: list[dict]) -> dict:
        started = time.time()
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": self.temperature},
        )
        elapsed_ms = (time.time() - started) * 1000
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") or {}
        cost_raw = resp.headers.get("x-litellm-response-cost")
        try:
            cost = float(cost_raw) if cost_raw not in (None, "") else None
        except ValueError:
            cost = None
        return {
            "text": (body["choices"][0]["message"].get("content") or ""),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost": cost,
            "latency_ms": elapsed_ms,
        }


# --------------------------------------------------------------------------------------
# 产出解析与判分
# --------------------------------------------------------------------------------------
_FILE_HINT = re.compile(r"^\s*(?:#|//|--)\s*file:\s*([A-Za-z0-9_./-]+)\s*$", re.MULTILINE)
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", re.DOTALL)


_CODE_HINT = re.compile(r"^\s*(?:def |class |import |from |@|//|/\*|package |public |function )", re.MULTILINE)


def extract_code_blocks(text: str, default_name: str = "solution.py") -> dict[str, str]:
    """从模型输出提取代码块 → {文件名: 代码}。

    文件名取自块内第一行的 `# file: xxx.py` 注释；没有则用 default_name，
    多个无名块按出现顺序命名为 default、xxx_2.py …

    **容忍模型只给裸代码（不加 ``` 围栏）**——真实观测：deepseek-chat 会严格按
    "第一行写 # file: solution.py" 直接输出代码，若只认围栏块会把它误判成 0 分。
    """
    text = text or ""
    blocks: dict[str, str] = {}
    for index, match in enumerate(_FENCE.finditer(text)):
        body = match.group(1)
        hint = _FILE_HINT.search(body)
        if hint:
            name = hint.group(1)
            body = body.replace(hint.group(0), "", 1).lstrip("\n")
        elif index == 0:
            name = default_name
        else:
            stem, _, ext = default_name.rpartition(".")
            name = f"{stem}_{index + 1}.{ext}" if stem else f"{default_name}_{index + 1}"
        blocks[name] = body
    if blocks:
        return blocks

    stripped = text.strip()
    if not stripped:
        return {}
    hint = _FILE_HINT.search(stripped)
    if hint:  # 裸代码 + 文件名提示
        return {hint.group(1): stripped.replace(hint.group(0), "", 1).strip("\n")}
    if _CODE_HINT.search(stripped):  # 裸代码但没写文件名
        return {default_name: stripped}
    return {}


def _strip_fences(text: str) -> str:
    """结构化产出（JSON）容错：模型偶尔仍包 ```json```，先剥掉再解析。"""
    stripped = (text or "").strip()
    match = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def _parse_pytest_counts(stdout: str) -> dict:
    passed = int((re.search(r"(\d+) passed", stdout) or [0, 0])[1]) if re.search(r"(\d+) passed", stdout) else 0
    failed = int((re.search(r"(\d+) failed", stdout) or [0, 0])[1]) if re.search(r"(\d+) failed", stdout) else 0
    errors = int((re.search(r"(\d+) error", stdout) or [0, 0])[1]) if re.search(r"(\d+) error", stdout) else 0
    return {"passed": passed, "failed": failed, "errors": errors}


def _run_pytest(runner, workdir: str, target: str) -> dict:
    cmd = [runner.python(), "-m", "pytest", "-q", target, "-p", "no:cacheprovider"]
    result = runner.run(cmd, workdir, timeout=SANDBOX_TIMEOUT)
    counts = _parse_pytest_counts(f"{result.get('stdout', '')}\n{result.get('stderr', '')}")
    counts.update({"returncode": result.get("returncode"), "stdout": (result.get("stdout") or "")[-2000:],
                   "stderr": (result.get("stderr") or "")[-1000:]})
    return counts


def _score_hidden_tests(check: dict, output: str, runner) -> dict:
    files = extract_code_blocks(output)
    if not files:
        return {"passed": False, "detail": "输出中没有可提取的代码块"}
    with tempfile.TemporaryDirectory(prefix="eval-hidden-") as workdir:
        for name, code in files.items():
            path = Path(workdir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code, encoding="utf-8")
        (Path(workdir) / "test_hidden.py").write_text(check["tests"], encoding="utf-8")
        run = _run_pytest(runner, workdir, "test_hidden.py")
    collected = run["passed"] + run["failed"]
    total = collected or run["errors"]
    score = (run["passed"] / total) if total else 0.0
    detail = f"隐藏测试 {run['passed']}/{total} 通过（returncode={run['returncode']}）"
    if collected == 0:
        detail += "；用例未收集到（生成代码导入失败/语法错误）"
    return {
        "passed": score == 1.0,
        "score": round(score, 4),
        "detail": detail,
        "evidence": run["stdout"][-600:] if score < 1.0 else "",
        "files": sorted(files),
    }


def _score_mutation_tests(check: dict, output: str, runner) -> dict:
    """用例质量：正确实现必须全绿；含 bug 实现必须变红（抓不到 mutant 只给一半分）。"""
    files = extract_code_blocks(output, default_name="test_generated.py")
    if not files:
        return {"passed": False, "score": 0.0, "detail": "输出中没有可提取的用例代码块"}
    detail: dict[str, Any] = {"files": sorted(files)}

    def _run_with(impl: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix="eval-mutation-") as workdir:
            for name, code in {**files, **impl}.items():
                path = Path(workdir) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(code, encoding="utf-8")
            target = "test_generated.py" if "test_generated.py" in files else sorted(files)[0]
            return _run_pytest(runner, workdir, target)

    correct = _run_with(check["correct_files"])
    detail["correct_run"] = {k: correct[k] for k in ("passed", "failed", "errors", "returncode")}
    if correct["returncode"] != 0 or correct["passed"] == 0:
        detail.update({"passed": False, "score": 0.0,
                       "detail": "生成的用例在正确实现上就失败（用例本身有问题）",
                       "evidence": correct["stdout"][-600:]})
        return detail

    buggy = _run_with(check["buggy_files"])
    detail["buggy_run"] = {k: buggy[k] for k in ("passed", "failed", "errors", "returncode")}
    killed = buggy["returncode"] != 0 and (buggy["failed"] + buggy["errors"]) > 0
    detail.update(
        {
            "passed": killed,
            "score": 1.0 if killed else 0.5,
            "detail": (
                f"用例在正确实现上全绿（{correct['passed']} passed），"
                + ("并成功捕获含 bug 实现 ✅" if killed else "但在含 bug 实现上仍全绿 ❌（用例太弱）")
            ),
            "evidence": buggy["stdout"][-600:] if not killed else "",
        }
    )
    return detail


def _validate_json_schema(value: Any, schema: dict, path: str = "$") -> list[str]:
    """JSON Schema 子集校验：type / properties / required / items / enum / min_items。"""
    errors: list[str] = []
    expected = schema.get("type")
    type_map = {
        "object": dict, "array": list, "string": str,
        "number": (int, float), "integer": int, "boolean": bool,
    }
    if expected:
        py_type = type_map.get(expected)
        if py_type and (not isinstance(value, py_type) or isinstance(value, bool) and expected != "boolean"):
            return [f"{path}: 期望 {expected}，实际 {type(value).__name__}"]
    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 缺少必填字段")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors += _validate_json_schema(value[key], sub, f"{path}.{key}")
    elif expected == "array":
        min_items = schema.get("min_items")
        if min_items is not None and len(value) < min_items:
            errors.append(f"{path}: 至少需要 {min_items} 项，实际 {len(value)}")
        items = schema.get("items")
        if items:
            for index, item in enumerate(value):
                errors += _validate_json_schema(item, items, f"{path}[{index}]")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {value!r} 不在允许集合 {schema['enum']}")
    return errors


def _score_json_schema(check: dict, output: str, runner) -> dict:
    raw = _strip_fences(output)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"passed": False, "detail": f"输出不是合法 JSON（{exc}）", "evidence": raw[:300]}
    errors = _validate_json_schema(data, check["schema"])
    return {
        "passed": not errors,
        "detail": "JSON Schema 校验通过" if not errors else f"Schema 不满足：{'; '.join(errors[:5])}",
        "evidence": "" if not errors else json.dumps(data, ensure_ascii=False)[:400],
    }


def _score_required_sections(check: dict, output: str, runner) -> dict:
    text = output or ""
    missing = []
    for section in check["sections"]:
        # 允许 #/##/### 任意级别，标题文本包含即可
        if not re.search(rf"^#{{1,4}}\s*.*{re.escape(section)}", text, re.MULTILINE):
            missing.append(section)
    return {
        "passed": not missing,
        "detail": "必备小节齐备" if not missing else f"缺少小节：{', '.join(missing)}",
        "evidence": "" if not missing else text[:300],
    }


def _score_regex_any(check: dict, output: str, runner) -> dict:
    matches = re.findall(check["pattern"], output or "", re.IGNORECASE | re.MULTILINE)
    need = int(check.get("min_matches", 1))
    return {
        "passed": len(matches) >= need,
        "detail": f"匹配 {len(matches)} 处（要求 ≥{need}）：{check.get('label', check['pattern'])}",
    }


# 否定语境：模型把禁止项明确列为"不建议/禁止"时不应算违规
# （真实观测：deepseek-chat 写"不建议忽略告警"，被朴素子串匹配误判成违规）
# 只认"直接修饰该动作"的否定表达；裸"不"会误伤"不过/不但"，故不收
_NEGATIONS = ("不建议", "不应", "不要", "不宜", "不得", "禁止", "避免", "切勿", "严禁", "无需", "拒绝")
_NEGATION_WINDOW = 8


def _score_forbidden_regex(check: dict, output: str, runner) -> dict:
    """禁止表述检查（否定语境感知）。

    若禁止词出现在否定语境（前 8 字符内有"不/禁止/避免/切勿/严禁"等），
    视为模型在**声明禁止项**而非给出该处置，不计违规。
    """
    text = output or ""
    pattern = re.compile(check["pattern"], re.IGNORECASE | re.MULTILINE)
    violations, negated = [], []
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - _NEGATION_WINDOW): match.start()]
        if any(token in prefix for token in _NEGATIONS):
            negated.append(match.group(0))
        else:
            violations.append(match)
    if not violations:
        detail = "未出现禁止表述"
        if negated:
            detail += f"（{len(negated)} 处出现在否定语境中，如「不建议{negated[0]}」，不算违规）"
        return {"passed": True, "detail": detail}
    first = violations[0]
    return {
        "passed": False,
        "detail": f"命中禁止表述：{first.group(0)!r}（{check.get('label', '')}），共 {len(violations)} 处",
        "evidence": text[max(0, first.start() - 60): first.end() + 60],
    }


def _score_required_keywords(check: dict, output: str, runner) -> dict:
    text = (output or "").lower()
    hits = [kw for kw in check["keywords"] if kw.lower() in text]
    ok = bool(hits) if check.get("match", "all") == "any" else len(hits) == len(check["keywords"])
    return {
        "passed": ok,
        "detail": f"命中关键词 {hits or '无'}（要求 {check.get('match', 'all')}：{check['keywords']}）",
    }


SCORERS: dict[str, Callable[[dict, str, Any], dict]] = {
    "hidden_tests": _score_hidden_tests,
    "mutation_tests": _score_mutation_tests,
    "json_schema": _score_json_schema,
    "required_sections": _score_required_sections,
    "regex_any": _score_regex_any,
    "forbidden_regex": _score_forbidden_regex,
    "required_keywords": _score_required_keywords,
}


def score_task(task: GoldenTask, output: str, runner=None) -> dict:
    """逐条 check 判分；任务分为各 check 的平均（每条 check 等权，避免主观加权）。"""
    runner = runner or get_runner()
    details = []
    for check in task.checks:
        try:
            result = SCORERS[check["type"]](check, output, runner)
        except Exception as exc:  # 判分器自身出错不能吞掉，但也不该炸掉整轮评估
            result = {"passed": False, "score": 0.0, "detail": f"判分器异常：{type(exc).__name__}: {exc}"}
        result.setdefault("score", 1.0 if result.get("passed") else 0.0)
        result["type"] = check["type"]
        result["label"] = check.get("label") or check["type"]
        details.append(result)
    score = sum(d["score"] for d in details) / len(details) if details else 0.0
    return {"score": round(score, 4), "passed": all(d["passed"] for d in details), "checks": details}


# --------------------------------------------------------------------------------------
# 评测执行与报告
# --------------------------------------------------------------------------------------
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (len(ordered) - 1) * pct
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 1)


def run_eval(
    tasks: list[GoldenTask],
    models: list[str],
    llm_factory: Callable[[str], Any] = HttpLLM.from_env,
    runner=None,
    judge: Optional[Callable[[GoldenTask, str], dict]] = None,
    limit: Optional[int] = None,
    system_default: str = "",
) -> dict:
    """对每个模型跑一遍金标集；单个任务失败只记 0 分，不中断整轮。

    `judge` 为可选的 LLM 评审（质量辅助信号），其结论单独存放并在报告中标注非唯一依据。
    """
    runner = runner or get_runner()
    selected = tasks[:limit] if limit else tasks
    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": None,
        "judge_enabled": judge is not None,
        "runner_mode": getattr(runner, "mode", "unknown"),
        "tasks": [{"id": t.id, "role": t.role, "weight": t.weight, "checks": [c["type"] for c in t.checks]} for t in selected],
        "models": [],
        "results": [],
    }
    for model in models:
        try:
            llm = llm_factory(model)
            report["endpoint"] = getattr(llm, "base_url", None) or report["endpoint"]
        except Exception as exc:
            report["models"].append({"model": model, "error": f"客户端初始化失败：{exc}"})
            continue
        latencies: list[float] = []
        costs: list[float] = []
        unmetered = 0
        prompt_tokens = completion_tokens = 0
        scores: list[tuple[float, float]] = []
        task_rows = []
        for task in selected:
            messages = []
            if task.system or system_default:
                messages.append({"role": "system", "content": task.system or system_default})
            messages.append({"role": "user", "content": task.prompt})
            row: dict = {"model": model, "task_id": task.id, "role": task.role, "weight": task.weight}
            try:
                call = llm.chat(messages)
            except Exception as exc:
                row.update({"score": 0.0, "passed": False, "error": f"{type(exc).__name__}: {exc}",
                            "checks": [], "output_chars": 0})
                task_rows.append(row)
                scores.append((0.0, task.weight))
                continue
            latencies.append(call.get("latency_ms", 0.0))
            if call.get("cost") is None:
                unmetered += 1
            else:
                costs.append(call["cost"])
            prompt_tokens += call.get("prompt_tokens", 0)
            completion_tokens += call.get("completion_tokens", 0)
            verdict = score_task(task, call.get("text", ""), runner=runner)
            row.update(
                {
                    "score": verdict["score"],
                    "passed": verdict["passed"],
                    "checks": verdict["checks"],
                    "latency_ms": round(call.get("latency_ms", 0.0), 1),
                    "total_tokens": call.get("total_tokens", 0),
                    "cost": call.get("cost"),
                    "output_chars": len(call.get("text", "")),
                    "output_head": call.get("text", "")[:400],
                }
            )
            if judge is not None:
                try:
                    row["judge"] = judge(task, call.get("text", ""))
                except Exception as exc:
                    row["judge"] = {"error": f"{type(exc).__name__}: {exc}"}
            task_rows.append(row)
            scores.append((verdict["score"], task.weight))
        total_weight = sum(w for _, w in scores) or 1.0
        weighted = sum(s * w for s, w in scores) / total_weight
        report["results"] += task_rows
        model_row = {
            "model": model,
            "tasks": len(selected),
            "passed": sum(1 for r in task_rows if r.get("passed")),
            "pass_rate": round(sum(1 for r in task_rows if r.get("passed")) / len(selected), 4) if selected else 0.0,
            "avg_score": round(weighted, 4),
            "latency_p50_ms": _percentile(latencies, 0.5),
            "latency_p95_ms": _percentile(latencies, 0.95),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": round(sum(costs), 6) if costs else None,
            "cost_status": "metered" if not unmetered else ("unmetered" if not costs else "partial"),
            "unmetered_calls": unmetered,
        }
        report["models"].append(model_row)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def _fmt_cost(model_row: dict) -> str:
    if model_row.get("cost_usd") is None:
        return "unmetered（网关未返回计费）"
    suffix = "" if model_row.get("cost_status") == "metered" else f"（部分计量：{model_row['unmetered_calls']} 次无计费头）"
    return f"${model_row['cost_usd']:.6f}{suffix}"


def render_markdown(report: dict) -> str:
    lines = [
        "# 模型评估报告（离线金标集 + 沙箱判分）",
        "",
        f"- 评估时间：{report.get('started_at')} → {report.get('finished_at')}",
        f"- 评估端点：`{report.get('endpoint')}`（`EVAL_BASE_URL` 可指向私有化/自建端点）",
        f"- 执行沙箱：`{report.get('runner_mode')}`（hidden_tests / mutation_tests 在其中运行）",
        f"- LLM 评审（judge）：{'已开启（辅助信号，非唯一评分依据）' if report.get('judge_enabled') else '未开启（纯机器判分）'}",
        f"- 任务数：{len(report.get('tasks', []))}",
        "",
        "## 1. 模型横向对比",
        "",
        "| 模型 | 通过率 | 加权得分 | 延迟 P50 | 延迟 P95 | tokens | 成本 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report.get("models", []):
        if row.get("error"):
            lines.append(f"| {row['model']} | — | — | — | — | — | 初始化失败：{row['error']} |")
            continue
        lines.append(
            f"| `{row['model']}` | {row['passed']}/{row['tasks']}（{row['pass_rate']:.0%}） | {row['avg_score']:.3f} "
            f"| {row['latency_p50_ms']:.0f} ms | {row['latency_p95_ms']:.0f} ms | {row['total_tokens']} | {_fmt_cost(row)} |"
        )

    lines += ["", "## 2. 逐任务得分", "", "| 任务 | 角色 | 模型 | 得分 | 结果 | 关键判据 |", "|---|---|---|---|---|---|"]
    for row in report.get("results", []):
        if row.get("error"):
            lines.append(f"| {row['task_id']} | {row['role']} | `{row['model']}` | 0.000 | ❌ 调用失败 | {row['error'][:80]} |")
            continue
        failed = [c for c in row.get("checks", []) if not c.get("passed")]
        summary = "；".join(f"{c['label']}:{c['detail']}" for c in (failed or row.get("checks", []))[:2])
        lines.append(
            f"| {row['task_id']} | {row['role']} | `{row['model']}` | {row['score']:.3f} "
            f"| {'✅' if row['passed'] else '❌'} | {summary[:160]} |"
        )

    lines += ["", "## 3. 失败明细（可直接复核证据）", ""]
    any_failure = False
    for row in report.get("results", []):
        failures = [c for c in (row.get("checks") or []) if not c.get("passed")]
        if not failures and not row.get("error"):
            continue
        any_failure = True
        lines.append(f"### {row['task_id']} · `{row['model']}`（得分 {row.get('score', 0):.3f}）")
        if row.get("error"):
            lines.append(f"- 调用失败：{row['error']}")
        for check in failures:
            lines.append(f"- ❌ **{check['label']}**（{check['type']}）：{check['detail']}")
            if check.get("evidence"):
                lines.append(f"  ```\n  {check['evidence'][:500]}\n  ```")
        lines.append("")
    if not any_failure:
        lines.append("（本轮无失败项）")

    lines += [
        "",
        "## 4. 口径与限制（诚实声明）",
        "",
        "- 判分为**机器判定**：代码类在沙箱跑隐藏测试（通过比例）、用例类用变异检验（能不能抓到 bug）、",
        "  结构化产出校验 JSON Schema、文档/诊断校验必备小节与关键表述；均不依赖模型自评。",
        "- 成本仅取网关 `x-litellm-response-cost` 头；**拿不到就标 unmetered，不做估算**",
        "  （若某模型长期 unmetered，请在 litellm-config.yaml 用 model_info 显式声明单价，",
        "   历史调用不回流，可用 scripts/qwen_cost_audit.py 量化缺口）。",
        "- **同模型重复运行结果可能不同**：实测 qwen-max 在 temperature=0 下两轮分别为 10/10 与 8/10，",
        "  故单轮结论用于排除明显不适用者；精细对比需多轮取均值（当前夹具未内置重复运行）。",
        "- 延迟为端到端单次调用耗时（含网关开销），受网络与网关排队影响，仅供同轮横向比较。",
        f"- 金标集规模有限（当前 {len(report.get('tasks', []))} 个任务），结论用于**排除明显不适用的模型**，不构成精细排名。",
        "- 私有化模型接入：设置 `EVAL_BASE_URL` / `EVAL_API_KEY` / `EVAL_MODEL` 指向任意 OpenAI 兼容端点即可复用本报告。",
        "",
    ]
    return "\n".join(lines)


def write_report(out_dir: str | Path, report: dict, tag: str = "") -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = f"model-eval-{tag + '-' if tag else ''}{stamp}"
    md_path = out / f"{stem}.md"
    json_path = out / f"{stem}.json"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
