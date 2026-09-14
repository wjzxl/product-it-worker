#!/usr/bin/env python3
"""Phase 3 扩展：Prompt 版本化 + 知识库自动优化的真实应用验证。

链路：挖掘建议 → 提案（prompt_version / knowledge_ingest）→ G7 人审 → 应用 → **热更新生效验证**
  · Prompt：agent-runtime `/prompts` 显示新 active 版本，且加载内容为新 Prompt（无需重启）
  · 知识：knowledge-base 检索能命中新入库文档（带来源）

用法：
  export GITLAB_TOKEN=... PROJECT_ID=85697055
  services/process-mining/.venv/bin/python scripts/apply_optimizations.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
PG = "postgresql+psycopg://platform:platform@127.0.0.1:5432/platform"
PORTS = {"approval-gateway": 8040, "process-mining": 8041, "agent-runtime": 8042, "knowledge-base": 8043, "gitlab-bridge": 8044}
TMP = tempfile.mkdtemp(prefix="apply-opt-")

procs: list[subprocess.Popen] = []
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def python_of(service: str) -> str:
    venv = os.path.join(SERVICES, service, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def start() -> None:
    env = dict(os.environ)
    env.update({"DATABASE_URL": PG, "SOP_CONFIG_PATH": os.path.join(ROOT, "sop-config.yaml"), "AUTH_MODE": "off"})
    procs.append(subprocess.Popen(
        [python_of("approval-gateway"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["approval-gateway"])],
        cwd=os.path.join(SERVICES, "approval-gateway"), env=env,
        stdout=open(f"{TMP}/approval.log", "w"), stderr=subprocess.STDOUT))

    env = dict(os.environ)
    env.update({
        "PLATFORM_DSN": "host=127.0.0.1 port=5432 dbname=platform user=platform password=platform",
        "LITELLM_DSN": "host=127.0.0.1 port=5432 dbname=litellm user=platform password=platform",
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['gitlab-bridge']}",
        "KNOWLEDGE_BASE_URL": f"http://127.0.0.1:{PORTS['knowledge-base']}",
        "PROMPTS_DIR": os.path.join(ROOT, "prompts"),
        "SOP_CONFIG_PATH": os.path.join(ROOT, "sop-config.yaml"),
        "PROJECT_ID": os.getenv("PROJECT_ID", ""),
        "LLM_BASE_URL": "http://127.0.0.1:4000/v1",
        "LLM_API_KEY": "sk-litellm-master",
        "LLM_MODEL": "qwen-max",
    })
    procs.append(subprocess.Popen(
        [python_of("process-mining"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["process-mining"])],
        cwd=os.path.join(SERVICES, "process-mining"), env=env,
        stdout=open(f"{TMP}/mining.log", "w"), stderr=subprocess.STDOUT))

    env = dict(os.environ)
    env.update({
        "LLM_API_KEY": "sk-litellm-master", "LLM_BASE_URL": "http://127.0.0.1:4000/v1",
        "LLM_MODEL": "qwen-max", "PROMPTS_DIR": os.path.join(ROOT, "prompts"),
        "APPROVAL_GATEWAY_URL": f"http://127.0.0.1:{PORTS['approval-gateway']}",
        "GITLAB_BRIDGE_URL": f"http://127.0.0.1:{PORTS['gitlab-bridge']}",
        "KNOWLEDGE_BASE_URL": f"http://127.0.0.1:{PORTS['knowledge-base']}",
    })
    procs.append(subprocess.Popen(
        [python_of("agent-runtime"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["agent-runtime"])],
        cwd=os.path.join(SERVICES, "agent-runtime"), env=env,
        stdout=open(f"{TMP}/runtime.log", "w"), stderr=subprocess.STDOUT))

    env = dict(os.environ)
    env.update({
        "LLM_API_KEY": "sk-litellm-master", "LLM_BASE_URL": "http://127.0.0.1:4000/v1",
        "EMBEDDING_MODEL": "qwen-embed", "EMBEDDING_DIM": "1024",
        "QDRANT_URL": "http://localhost:6333", "KB_COLLECTION": "runbooks_kb",
    })
    procs.append(subprocess.Popen(
        [python_of("knowledge-base"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["knowledge-base"])],
        cwd=os.path.join(SERVICES, "knowledge-base"), env=env,
        stdout=open(f"{TMP}/kb.log", "w"), stderr=subprocess.STDOUT))

    env = dict(os.environ)
    env.update({"GITLAB_URL": os.getenv("GITLAB_URL", "https://gitlab.com"),
                "GITLAB_TOKEN": os.getenv("GITLAB_TOKEN", ""), "WEBHOOK_SECRET": "apply-opt"})
    procs.append(subprocess.Popen(
        [python_of("gitlab-bridge"), "-m", "uvicorn", "app.main:app", "--port", str(PORTS["gitlab-bridge"])],
        cwd=os.path.join(SERVICES, "gitlab-bridge"), env=env,
        stdout=open(f"{TMP}/bridge.log", "w"), stderr=subprocess.STDOUT))


def wait_healthy() -> bool:
    deadline = time.time() + 90
    for name, port in PORTS.items():
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            check(f"服务 {name} 启动", False)
            return False
    return True


def apply_proposal(proposal: dict, decided_by: str) -> dict:
    """提交 → G7 审批 → 批准 → 应用。"""
    sub = httpx.post(f"http://127.0.0.1:{PORTS['process-mining']}/mining/proposals/submit",
                     json={"proposal": proposal, "requester": "流程挖掘引擎", "task_ref": "phase3-ext"}, timeout=60).json()
    httpx.post(f"http://127.0.0.1:{PORTS['approval-gateway']}/approvals/{sub['approval_id']}/decision",
               json={"decision": "approve", "decided_by": decided_by, "comment": "依据充分，批准应用"}, timeout=30)
    return httpx.post(f"http://127.0.0.1:{PORTS['process-mining']}/mining/proposals/apply",
                      json={"proposal": proposal, "approval_id": sub["approval_id"]}, timeout=180).json()


def main() -> int:
    start()
    print("服务启动中…（prompt 注册中心 + 知识库 + G7 闸门）")
    if not wait_healthy():
        return 1
    check("五个服务健康", True)

    # 状态无关：脚本可反复执行（版本号可能已被上一轮推进，不假定一定是 v1）
    before = httpx.get(f"http://127.0.0.1:{PORTS['agent-runtime']}/prompts", timeout=30).json()
    ver_before = before["prompts"].get("coder_agent", "")
    check("① 初始 Prompt 版本可读", bool(ver_before), f"coder_agent={ver_before}")
    expect_next = f"v{int(ver_before.lstrip('v') or 1) + 1}"

    # 提案 A：升级编码 Agent Prompt（补充"最小化改动/禁止占位注释"约束——源自 Phase 1/2 真实教训）
    # 内容带本轮标记：既保证每次演练的版本内容唯一可辨（指纹必然变化），也便于溯源是哪一轮固化的
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_prompt = (
        open(os.path.join(ROOT, "prompts", "coder_agent.v1.md"), encoding="utf-8").read().strip()
        + "\n- 外科手术式修改：只改动必要的实现行，禁止用占位注释替换函数体，禁止重写无关文件；\n"
        + "- 保持既有函数签名与返回值类型不变（既有测试必须继续通过）；\n"
        + f"- （本轮固化标记：{stamp}，来源：G7 流程改进闸门演练）"
    )
    proposal_a = {
        "id": "prop-prompt-1",
        "title": "升级编码 Agent Prompt：最小化改动约束",
        "rationale": "演练中多次出现重写文件/占位注释替换函数体导致的返工，应在 Prompt 层固化约束",
        "evidence": "Phase 1/2 演练：Qwen/DeepSeek 均出现过函数体被占位注释替换、返回值类型被改变",
        "action": {"type": "prompt_version", "target": "coder_agent", "value": new_prompt},
        "risk": "L2",
        "appliable": True,
    }
    res_a = apply_proposal(proposal_a, "平台负责人@Grace")
    check("② Prompt 新版本已写入并切换 active",
          res_a["apply"]["applied"] and res_a["apply"]["after"] == expect_next,
          f"{res_a['apply'].get('before')} → {res_a['apply'].get('after')}（期望 {expect_next}）")

    after = httpx.get(f"http://127.0.0.1:{PORTS['agent-runtime']}/prompts", timeout=30).json()
    meta_before = before["loaded_meta"].get("coder_agent", {})
    meta_after = after["loaded_meta"].get("coder_agent", {})
    check("③ agent-runtime 热更新生效（无需重启）",
          after["prompts"].get("coder_agent") == expect_next
          and meta_after.get("sha256") != meta_before.get("sha256")
          and meta_after.get("length", 0) >= meta_before.get("length", 0),
          f"{ver_before}(len={meta_before.get('length')}, sha={meta_before.get('sha256')}) → "
          f"{expect_next}(len={meta_after.get('length')}, sha={meta_after.get('sha256')})")
    check("④ 改进项已登记 GitLab",
          bool(res_a["improvement_issue"].get("created")),
          f"issue #{res_a['improvement_issue'].get('issue_iid')}")

    # 提案 B：知识库自动入库（把演练中确认有效的 Runbook 经验固化）
    knowledge = (
        "# Runbook 增补：导出接口日期范围与多租户数据清理\n\n"
        "## 日期范围上限（已验证有效）\n"
        "- 症状：导出接口 p95 超过 SLO 3s，日志显示日期跨度数月~数年\n"
        "- 处置：`fetch_report_data(start, end)` 增加 365 天上限校验，超限返回空列表；保持返回值类型 `List[List[str]]` 不变\n"
        "- 验证：修复后 p95 < 3s，超限请求 < 200ms 返回\n\n"
        "## 多租户数据清理规范\n"
        "- 多租户验证产生的临时审批单必须显式决策关闭（避免污染信任指标与挖掘统计）\n"
        "- 清理操作使用独立审批人标识（如 cleanup@platform），便于流程挖掘区分演练噪声\n"
    )
    proposal_b = {
        "id": "prop-kb-1",
        "title": "固化导出接口与多租户清理 Runbook",
        "rationale": "把演练中验证有效的处置经验入库，供运维域 RAG 诊断直接引用",
        "evidence": "Phase 2 演练：365 天范围校验修复使 MR !16 合入；多租户清理曾污染 G1 信任指标",
        "action": {"type": "knowledge_ingest", "target": "runbooks", "value": knowledge},
        "risk": "L2",
        "appliable": True,
    }
    res_b = apply_proposal(proposal_b, "平台负责人@Grace")
    check("⑤ 知识已自动入库", res_b["apply"]["applied"], f"{res_b['apply'].get('source')}（{res_b['apply'].get('chunks')} 块）")

    hits = httpx.post(f"http://127.0.0.1:{PORTS['knowledge-base']}/search",
                      json={"query": "导出接口日期范围上限 365 天 多租户清理规范", "limit": 3}, timeout=60).json()
    auto_hit = [h for h in hits if h["source"].startswith("runbooks/auto-")]
    check("⑥ 运维域检索可命中新入库知识", bool(auto_hit),
          f"top={hits[0]['source'] if hits else '-'}" + (f"｜命中自动入库 {len(auto_hit)} 条" if auto_hit else ""))
    check("⑦ 改进项已登记 GitLab",
          bool(res_b["improvement_issue"].get("created")),
          f"issue #{res_b['improvement_issue'].get('issue_iid')}")

    passed = all(ok for _, ok, _ in results)
    print(f"\n{'=' * 52}\n扩展应用动作验证：{passed and '全部通过 🎉' or '存在失败 ❌'}（{sum(1 for _, ok, _ in results if ok)}/{len(results)}）")
    return 0 if passed else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)
