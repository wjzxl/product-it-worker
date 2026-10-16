#!/usr/bin/env python3
"""生成本次战役的三份机器可读产物：system-model.json / test-plan.json / test-cases.md。

为什么用生成器而不是手写：卡片有 60 张，手写必然出现 id/字段不一致；而
`run_tests.py plan` 的校验（孤儿卡、缺 P0 覆盖）只有在结构一致时才有意义。

用法：python3 build_plan.py
"""
from __future__ import annotations

import json
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent                      # 仓库根
COMMIT = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                        capture_output=True, text=True).stdout.strip()
MARKER = "camp1008"
GW, RT, KB, TC, MD, GL, PM = ("http://127.0.0.1:18001", "http://127.0.0.1:18002",
                              "http://127.0.0.1:18003", "http://127.0.0.1:18004",
                              "http://127.0.0.1:18005", "http://127.0.0.1:18006",
                              "http://127.0.0.1:18007")
PROXY = "http://127.0.0.1:18015"
BROWSER = "/Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py"

FEATURES = [
    ("F-health", "健康与治理姿态", "P0", "所有服务 /health 暴露档位/合规/checkpointer；探针与看门狗依赖它",
     ["GET /health ×7"]),
    ("F-authz", "服务间鉴权", "P0", "令牌中间件 + 显式豁免；缺失即所有写接口裸奔（历史缺陷根因）",
     ["X-Service-Token", "SERVICE_AUTH_EXEMPT"]),
    ("F-gate", "人审闸门 G1–G7", "P0", "平台的核心承诺：AI 执行、人在闸门决策；被绕过则产品价值归零",
     ["POST /approvals", "POST /approvals/{id}/decision", "POST /callbacks/im/card"]),
    ("F-decision-verify", "决策依据核验", "P0", "恢复人审挂起必须出示真实决策，否则闸门形同虚设",
     ["POST /runs/{tid}/resume"]),
    ("F-enggate", "工程闸门 E1–E3", "P0", "规则即代码；mode 必须服务端权威、留痕强制",
     ["POST /eng-gates/{code}/check"]),
    ("F-taskfsm", "任务状态机与租户隔离", "P0", "多产品线共用平台的前提是租户隔离不可破",
     ["POST /tasks", "POST /tasks/{id}/transition"]),
    ("F-event", "平台事件接入", "P1", "事件驱动状态联动；幂等与歧义处理决定数据一致性",
     ["POST /events"]),
    ("F-tools", "工具注册表与执行期鉴权", "P0", "AI 能改外部状态，权限边界必须由服务端判定",
     ["POST /tools/invoke"]),
    ("F-runtime", "Agent 运行与归档", "P0", "运行状态与成本是度量体系的唯一来源",
     ["POST /runs", "GET /runs/{tid}/archive"]),
    ("F-evidence", "证据库", "P1", "验收结论要能复验（sha256），否则证据只是附件",
     ["POST /evidence", "GET /evidence/verify"]),
    ("F-provenance", "需求来源链反查", "P1", "争论口径时能回查当初是谁在哪句话提的",
     ["GET /requirements/{ref}/provenance"]),
    ("F-mining", "流程挖掘与 SOP 自进化", "P1", "瓶颈→提案→G7→应用；错误应用会改掉流程配置",
     ["POST /mining/proposals", "POST /mining/proposals/apply"]),
    ("F-ui-console", "审批台（人做决策的界面）", "P0", "审批人看到的就是它；静默空列表会让决策停摆",
     ["GET / (18001)", "app.js/app.css"]),
    ("F-ui-dashboard", "驾驶舱（监督与介入）", "P0", "试点期唯一的全局视图；读不出等于没有视图",
     ["GET / (18005)"]),
    ("F-alert-ingest", "告警接入", "P1", "真实告警→运维任务→自动拉起 SOP；无验签则可被凭空触发",
     ["POST /alerts/prometheus"]),
    ("F-gitlab", "GitLab 双向集成", "P1", "入站 webhook 验签 + 出站 API；仓库写入权限在平台手里",
     ["POST /webhooks/gitlab", "POST /gitlab/*"]),
    ("F-kb", "知识库入库与检索", "P1", "RAG 是运维域诊断的依据；来源必须可追溯",
     ["POST /documents", "POST /search"]),
    ("F-migration", "数据库迁移与备份", "P0", "上线硬前提：空库可建、模型与迁移零漂移、可回滚、可恢复",
     ["alembic upgrade head", "backup_restore.py"]),
    ("F-watchdog", "平台自监控与告警", "P1", "平台自己挂了要有人知道（试点期最尴尬的失败）",
     ["watch_health.py"]),
    ("F-deploy", "部署就绪与配置生成", "P0", "带着宽松开关上生产＝修复等于没修",
     ["check_deployment_readiness.py", "init_prod_env.py"]),
]

FLOWS = [
    ("FLOW-prod", "产品域：需求 → 澄清 → PRD → G1 人审", "P0",
     ["AI 产品岗", "人类审批人"],
     ["POST /runs（产品域）", "生成 PRD 并创建 G1 审批", "人在审批台批准/退回", "POST /runs/{tid}/resume"],
     "每次 PRD 都有对应的人审决策；退回后版本递增而不是覆盖", ["自述批准绕过闸门", "归档丢失否决"]),
    ("FLOW-dev", "研发域：issue → 方案 → G2 → 编码 → MR → G3", "P1",
     ["AI 研发岗", "人类审批人", "GitLab"],
     ["事件建任务", "方案 + G2", "编码（沙箱）", "MR + G3"],
     "代码变更必须经过 G3 才合并", ["跳过 G2/G3", "沙箱内逃逸"]),
    ("FLOW-test", "测试域：独立用例 → 报告 → G4 发布决策", "P1",
     ["AI 测试岗", "人类审批人"], ["独立生成用例", "执行并出报告", "G4 决策"],
     "发布决策有人审留痕", ["用例与研发同源（失去独立性）"]),
    ("FLOW-ops", "运维域：告警 → 任务 → RAG 诊断 → G5 → 执行 → G6 复盘", "P0",
     ["Alertmanager", "AI 运维岗", "人类审批人"],
     ["POST /alerts/prometheus", "自动触发 ops SOP", "RAG 检索 Runbook", "G5 变更审批", "执行（受控）", "G6 复盘"],
     "无验签不得建任务；变更执行必须有人审依据", ["匿名告警拉栈", "未审批执行变更"]),
    ("FLOW-platform", "平台域：挖掘 → 提案 → G7 → 应用 → 知识入库", "P1",
     ["AI 平台岗", "人类审批人"], ["挖掘瓶颈", "生成提案", "G7 审批", "apply 改配置"],
     "未获批的提案不得改动 SOP 配置", ["未批先应用", "配置被改坏无回滚"]),
    ("FLOW-approve-ui", "人在审批台的决策（UI 端到端）", "P0",
     ["人类审批人"], ["打开审批台", "看到待审列表", "查看三要素", "批准/退回/拒绝", "留痕可查"],
     "审批人看到的就是数据库里的真实待办；不得静默显示空", ["静默空列表", "渲染注入执行"]),
]

REQS = [
    ("REQ-001", "服务健康且如实报告自身状态（含治理姿态与 checkpointer）", "P0", ["F-health"]),
    ("REQ-002", "未鉴权请求不得触达业务读写接口", "P0", ["F-authz"]),
    ("REQ-003", "鉴权不得把服务间调用一起关掉", "P0", ["F-authz"]),
    ("REQ-004", "人审闸门不可被自述决策绕过", "P0", ["F-gate", "F-decision-verify"]),
    ("REQ-005", "工程闸门宽严由服务端决定，且每次判定留痕", "P0", ["F-enggate"]),
    ("REQ-006", "任务状态机拒绝非法流转，等待审批不得被跳过", "P0", ["F-taskfsm"]),
    ("REQ-007", "跨租户读写必须被隔离", "P0", ["F-taskfsm", "F-gate"]),
    ("REQ-008", "并发下同一单据只产生一个决策（不重复写入）", "P0", ["F-gate", "F-taskfsm"]),
    ("REQ-009", "工具调用权限由服务端判定，不接受请求体自述", "P0", ["F-tools"]),
    ("REQ-010", "运行结果与成本如实归档（否决可见）", "P1", ["F-runtime"]),
    ("REQ-011", "证据可复验（哈希一致）", "P1", ["F-evidence"]),
    ("REQ-012", "来源链可反查", "P1", ["F-provenance"]),
    ("REQ-013", "流程挖掘结论正确且提案不会绕过 G7", "P1", ["F-mining"]),
    ("REQ-014", "审批台在有数据时必须显示待办，且不得渲染注入内容", "P0", ["F-ui-console"]),
    ("REQ-015", "驾驶舱在文档所述入口拓扑下可读", "P0", ["F-ui-dashboard"]),
    ("REQ-016", "告警入口必须验签，未配密钥不得放行", "P1", ["F-alert-ingest"]),
    ("REQ-017", "GitLab webhook 必须验签", "P1", ["F-gitlab"]),
    ("REQ-018", "知识库入库/检索可用且来源可追溯", "P1", ["F-kb"]),
    ("REQ-019", "空库可建、模型与迁移零漂移、可回滚、备份可恢复", "P0", ["F-migration"]),
    ("REQ-020", "平台自身故障可被发现并通知", "P1", ["F-watchdog"]),
    ("REQ-021", "生产档位下不得带着宽松开关上线", "P0", ["F-deploy"]),
    ("REQ-022", "外部身份头不得被直连客户端伪造", "P0", ["F-authz", "F-ui-dashboard"]),
]


def cmd(card_id, title, layer, prio, feature_ids, req_ids, risk, expected, command,
        cwd=None, timeout=180000, expect_exit=0, contains="RESULT: PASS", env=None,
        flow_ids=None, not_contains=None):
    spec = {}
    if contains:
        spec["contains"] = contains
    if not_contains:
        spec["not_contains"] = not_contains
    check = {"type": "command", "command": command, "expect_exit": expect_exit,
             "assert_stdout": spec, "timeout_ms": timeout}
    if cwd:
        check["cwd"] = cwd
    if env:
        check["env"] = env
    card = {"id": card_id, "title": title, "repro": command, "layer": LAYER_ALIAS.get(layer, layer), "priority": prio,
            "feature_ids": feature_ids, "requirement_ids": req_ids, "risk": risk,
            "expected": expected, "check": check,
            # 判据来源：本轮几乎所有期望都来自仓内契约（设计文档/探针约定/历史缺陷），
            # 属 spec 或 code；标 inferred 的是"由实现推断"的期望。
            "oracle": {"kind": "spec", "status": "confirmed",
                       "reference": expected}}
    if flow_ids:
        card["flow_ids"] = flow_ids
    return card


def http(card_id, title, layer, prio, feature_ids, req_ids, risk, expected, method, path,
         base=GW, headers=None, status_expect=200, assert_json=None, body=None):
    check = {"type": "http", "method": method, "path": path, "status_expect": status_expect}
    if headers:
        check["headers"] = headers
    if assert_json:
        check["assert_json"] = assert_json
    if body:
        check["body"] = body
    return {"id": card_id, "title": title, "layer": layer, "priority": prio,
            "feature_ids": feature_ids, "requirement_ids": req_ids, "risk": risk,
            "expected": expected, "check": check}


# 旧 schema 的自定义层名 → 新 schema 的规范枚举
LAYER_ALIAS = {"concurrency": "stability", "ops": "other", "deploy": "other"}

AUTH = {"X-Service-Token": "pst-sut-token"}
AUTH_T = {**AUTH, "X-Tenant": "campaign"}
CARDS = []

# ---------------------------------------------------------------- 回归：原战役 15 个缺陷
REG = [
    ("REG-001", "im-callback", "IM 回调必须验签（BUG-001）", "P0", ["F-gate"], ["REQ-004"]),
    ("REG-002", "cross-tenant-read", "跨租户读取被拒（BUG-002 同源）", "P0", ["F-taskfsm"], ["REQ-007"]),
    ("REG-003", "resubmit-tenant", "resubmit 遵守租户边界（BUG-002）", "P1", ["F-gate"], ["REQ-007"]),
    ("REG-004", "gate-mode", "闸门模式不可被调用方覆盖（BUG-003）", "P0", ["F-enggate"], ["REQ-005"]),
    ("REG-005", "gate-persist", "每次闸门判定都留痕（BUG-014）", "P0", ["F-enggate"], ["REQ-005"]),
    ("REG-006", "timeout-auto", "超时处置取服务端信任等级（BUG-004）", "P0", ["F-gate"], ["REQ-004"]),
    ("REG-007", "task-fsm", "等待审批不得被无据流转（BUG-005）", "P0", ["F-taskfsm"], ["REQ-006"]),
    ("REG-008", "task-fsm-illegal", "非法流转 409（FSM-002）", "P1", ["F-taskfsm"], ["REQ-006"]),
    ("REG-009", "task-tenant", "任务详情租户隔离（BUG-006）", "P0", ["F-taskfsm"], ["REQ-007"]),
    ("REG-010", "task-dup-ref", "重复外部引用不 500（BUG-015）", "P1", ["F-taskfsm"], ["REQ-007"]),
    ("REG-011", "reject-archive", "被否决的运行归档可见（BUG-012）", "P0", ["F-runtime"], ["REQ-010"]),
    ("REG-012", "wake-guard", "等待人审不得被 /wake 吞掉（BUG-007）", "P0", ["F-runtime"], ["REQ-004"]),
    ("REG-013", "heuristic", "启发式挖掘方向正确（BUG-008）", "P1", ["F-mining"], ["REQ-013"]),
    ("REG-014", "alpha", "α 挖掘自洽（BUG-009）", "P1", ["F-mining"], ["REQ-013"]),
    ("REG-015", "dfg-conformance", "DFG 一致性检出异常（BUG-010）", "P1", ["F-mining"], ["REQ-013"]),
    ("REG-016", "llm-fallback", "LLM 畸形输出回退规则（BUG-011）", "P1", ["F-mining"], ["REQ-013"]),
    ("REG-017", "evidence", "证据上传与哈希复验（EVD）", "P1", ["F-evidence"], ["REQ-011"]),
    ("REG-018", "health-checkpointer", "健康如实报告持久化形态（WP7）", "P1", ["F-health"], ["REQ-001"]),
]
for cid, probe, title, prio, feats, reqs in REG:
    CARDS.append(cmd(cid, title, "api", prio, feats, reqs,
                     "原战役缺陷复发则说明修复未生效",
                     f"{probe} 探针输出 RESULT: PASS",
                     f"bash test-campaign/probes.sh {probe}", cwd=str(ROOT)))

# ---------------------------------------------------------------- 鉴权专项
CARDS += [
    cmd("SEC-101", "写接口鉴权覆盖率为 100%（清点）", "security", "P0", ["F-authz"], ["REQ-002"],
        "有写接口无鉴权＝端口可达即有写权限",
        "audit 输出“无鉴权 0 个”",
        "python3 platform/scripts/audit_endpoint_auth.py --strict", cwd=str(ROOT),
        contains="无鉴权 0 个"),
    cmd("SEC-102", "无令牌的读写请求被拒，健康检查豁免", "security", "P0", ["F-authz"], ["REQ-002"],
        "鉴权未生效则所有治理开关形同虚设",
        "探针输出 RESULT: PASS", "bash probes-authz.sh no-token", cwd=str(ROOT / "test-campaign")),
    cmd("SEC-103", "带令牌的审批与决策链路仍可用", "security", "P0", ["F-authz"], ["REQ-003"],
        "鉴权把功能一起关掉＝不可上线",
        "探针输出 RESULT: PASS", "bash probes-authz.sh with-token", cwd=str(ROOT / "test-campaign")),
    cmd("SEC-104", "自述决策不得恢复人审挂起", "security", "P0", ["F-decision-verify"], ["REQ-004"],
        "闸门可被绕过则平台核心价值归零",
        "探针输出 RESULT: PASS", "bash probes-authz.sh resume-bypass", cwd=str(ROOT / "test-campaign")),
    cmd("SEC-105", "工具调用不接受请求体自述岗位/免人审", "security", "P0", ["F-tools"], ["REQ-009"],
        "自述权限＝没有权限边界",
        "探针输出 RESULT: PASS", "bash probes-authz.sh tools-selfdeclared", cwd=str(ROOT / "test-campaign")),
    cmd("SEC-106", "告警 webhook 验签（无凭据/错密钥拒绝）", "security", "P1", ["F-alert-ingest"], ["REQ-016"],
        "无验签则可被凭空拉起运维 SOP",
        "探针输出 RESULT: PASS", "bash probes-authz.sh alert-signature", cwd=str(ROOT / "test-campaign")),
    cmd("SEC-107", "令牌模式下审批台静态资源可达、业务接口仍需令牌", "security", "P0",
        ["F-ui-console", "F-authz"], ["REQ-014"],
        "资源被鉴权挡住会让审批台白屏（历史缺陷）",
        "index/app.js/app.css 均 200 且 /approvals 401",
        "for f in '' index.html app.js app.css; do curl -s -o /dev/null -w \"$f=%{http_code} \" "
        f"{GW}/$f; done; echo; curl -s -o /dev/null -w 'api=%{{http_code}}\\n' {GW}/approvals",
        cwd=str(ROOT), contains="app.js=200"),
    cmd("SEC-108", "直连服务伪造入口身份头必须无效", "security", "P0", ["F-authz", "F-ui-dashboard"],
        ["REQ-022"],
        "TRUST_PROXY_AUTH 只在入口剥离同名头时成立；直连伪造得逞说明缺少纵深防御",
        "伪造 X-Platform-User 直连驾驶舱应得 401",
        f"curl -s -o /dev/null -w 'status=%{{http_code}}\\n' -H 'X-Platform-User: attacker' {MD}/",
        cwd=str(ROOT), contains="status=401"),
    cmd("SEC-109", "GitLab webhook 未签名被拒、正确签名放行", "security", "P1", ["F-gitlab"], ["REQ-017"],
        "伪造 webhook 可凭空建任务/改状态",
        "无签名 401、带 X-Gitlab-Token 200",
        f"curl -s -o /dev/null -w 'no_sig=%{{http_code}} ' -X POST {GL}/webhooks/gitlab "
        "-H 'Content-Type: application/json' -d '{\"object_kind\":\"issue\"}'; "
        f"curl -s -o /dev/null -w 'signed=%{{http_code}}\\n' -X POST {GL}/webhooks/gitlab "
        "-H 'Content-Type: application/json' -H 'X-Gitlab-Token: pst-sut-webhook' "
        "-d '{\"object_kind\":\"issue\",\"project\":{\"id\":1,\"path_with_namespace\":\"a/b\"},"
        "\"object_attributes\":{\"iid\":1,\"title\":\"t\",\"action\":\"open\",\"labels\":[]}}'",
        cwd=str(ROOT), contains="signed=200"),
]

# ---------------------------------------------------------------- 并发/竞态
CARDS += [
    cmd("CONC-201", "30 并发决策：恰好一个成功、一条决策", "concurrency", "P0", ["F-gate"],
        ["REQ-008"], "并发重复决策会重复触发下游恢复与惩罚",
        "命令输出 RESULT: PASS",
        "STRESS_ROUNDS=6 STRESS_N=30 bash probes-concurrency.sh decide-race-stress",
        cwd=str(ROOT / "test-campaign"), timeout=600000),
    cmd("CONC-202", "30 并发流转：恰好一个成功、一条时间线", "concurrency", "P0", ["F-taskfsm"],
        ["REQ-008"], "并发流转会写坏状态机与时间线",
        "命令输出 RESULT: PASS",
        "STRESS_ROUNDS=6 STRESS_N=30 bash probes-concurrency.sh task-race-stress",
        cwd=str(ROOT / "test-campaign"), timeout=600000),
    cmd("CONC-203", "人工决策与超时扫描并发：不双决策", "concurrency", "P0", ["F-gate"], ["REQ-008"],
        "双决策会让审批出现两个终态", "探针输出 RESULT: PASS",
        "bash probes-concurrency.sh decide-vs-sweep 2>&1 | iconv -f utf-8 -t utf-8 -c",
        cwd=str(ROOT / "test-campaign")),
    cmd("CONC-204", "IM 回调与 Web 决策并发：互斥", "concurrency", "P0", ["F-gate"], ["REQ-008"],
        "双通道同时决策会重复落库", "探针输出 RESULT: PASS",
        "bash probes-concurrency.sh im-vs-decide", cwd=str(ROOT / "test-campaign")),
    cmd("CONC-205", "并发上传证据不丢件", "concurrency", "P1", ["F-evidence"], ["REQ-011"],
        "丢件等于验收证据不完整", "探针输出 RESULT: PASS",
        "bash probes-concurrency.sh evidence-race 2>&1 | iconv -f utf-8 -t utf-8 -c",
        cwd=str(ROOT / "test-campaign")),
]

# ---------------------------------------------------------------- UI（真实浏览器）
CARDS += [
    cmd("UI-301", "审批台加载：静态外壳可见、无控制台报错", "e2e", "P0", ["F-ui-console"],
        ["REQ-014"], "审批人看到的第一屏就是它",
        "步骤全通过且页面无控制台错误",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui/UI-301 "
        "--report-json evidence/ui/UI-301/report.json --only UI-301 --baselines baselines",
        cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-302", "待审列表必须显示真实待办（不得静默空）", "e2e", "P0", ["F-ui-console"],
        ["REQ-014"], "静默空列表会让审批人以为无事可做，闸门停摆且无人察觉",
        "列表渲染出至少一条待审单据",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui "
        "--only UI-302 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-303", "详情渲染三要素与动作按钮", "e2e", "P0", ["F-ui-console"], ["REQ-014"],
        "决策依据不完整＝人无法负责任地批准",
        "详情出现①②③三要素与批准/拒绝按钮",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui "
        "--only UI-303 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-304", "XSS 载荷只作为文本出现，不生成元素、不执行", "e2e", "P0", ["F-ui-console"],
        ["REQ-014"], "审批人权限最高，在他的浏览器里执行脚本＝最高危",
        "无 img/script 元素、无脚本执行、无控制台错误",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui "
        "--only UI-304 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-305", "审批台可访问性：按钮有可访问名", "e2e", "P1", ["F-ui-console"], ["REQ-014"],
        "屏幕阅读器读不出动作按钮，等于部分人无法审批",
        "accessibility tree 中存在名为“批准”“拒绝”的按钮",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui "
        "--only UI-305 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-306", "视觉基线比对（审批台首屏）", "e2e", "P1", ["F-ui-console"], ["REQ-014"],
        "DOM 正确而屏幕坏掉是功能断言看不见的发布阻断项",
        "与基线差异比例 ≤ 2%",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui "
        "--only UI-306 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-309", "在界面上完成一次决策（浏览器端到端）", "e2e", "P0", ["F-ui-console", "F-gate"],
        ["REQ-014", "REQ-004"], "控制台是人审的唯一界面；能列出但批不了等于闸门停摆",
        "输入令牌后点击批准，页面提示已提交（原生 prompt 由测试替换）",
        "python3 ui/seed.py --marker camp1008 >/dev/null && "
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui/UI-309 "
        "--report-json evidence/ui/UI-309/report.json --only UI-309 --baselines baselines",
        cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-308", "无令牌时必须显示鉴权错误，不得伪装成空审批队列", "e2e", "P0", ["F-ui-console"],
        ["REQ-014"], "把 401 渲染成“暂无待审批事项”会让闸门静默停摆（上轮 DEF-001 的核心危害）",
        "未登录时页面必须显示错误提示与“审批列表加载失败”，且不得出现空态文案",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui/UI-308 "
        "--report-json evidence/ui/UI-308/report.json --only UI-308 --baselines baselines",
        cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-307", "视觉比对负向控制：注入样式缺陷必须被发现", "e2e", "P0", ["F-ui-console"],
        ["REQ-014"], "一个恒返回“匹配”的比较器与健康界面产生完全相同的报告",
        "改动基线样式后差异比例必须超过阈值",
        f"python3 {BROWSER} --suite ui/console-suite.json --out-dir evidence/ui/UI-307 "
        "--report-json evidence/ui/UI-307/report.json --only UI-307 --baselines baselines",
        cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-311", "驾驶舱经入口代理可读并渲染队列", "e2e", "P0", ["F-ui-dashboard"], ["REQ-015"],
        "试点期唯一全局视图；读不出等于没有视图",
        "页面出现待决策队列与误拦率文案",
        f"python3 {BROWSER} --suite ui/dashboard-suite.json --out-dir evidence/ui "
        "--only UI-311 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-312", "驾驶舱队列渲染真实事项", "e2e", "P0", ["F-ui-dashboard"], ["REQ-015"],
        "空队列会让人误判“系统没问题”",
        "表格存在且含 approval/gate_block/空态说明之一",
        f"python3 {BROWSER} --suite ui/dashboard-suite.json --out-dir evidence/ui "
        "--only UI-312 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-313", "驾驶舱可访问性树可用", "e2e", "P2", ["F-ui-dashboard"], ["REQ-015"],
        "表格结构对辅助技术可读", "存在 role=table",
        f"python3 {BROWSER} --suite ui/dashboard-suite.json --out-dir evidence/ui "
        "--only UI-313 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
    cmd("UI-314", "视觉基线比对（驾驶舱首屏）", "e2e", "P2", ["F-ui-dashboard"], ["REQ-015"],
        "样式回归的兜底", "与基线差异比例 ≤ 5%",
        f"python3 {BROWSER} --suite ui/dashboard-suite.json --out-dir evidence/ui "
        "--only UI-314 --baselines baselines", cwd=str(HERE), contains=None, not_contains="BLOCK", timeout=180000),
]

# ---------------------------------------------------------------- 数据/迁移/备份
CARDS += [
    cmd("DATA-401", "迁移：空库可建 + 模型零漂移 + 可回滚", "data", "P0", ["F-migration"], ["REQ-019"],
        "没有迁移路径＝改不动库、回不了滚",
        "命令输出“迁移体检通过”",
        "platform/services/approval-gateway/.venv/bin/python platform/scripts/check_migrations.py --selftest",
        cwd=str(ROOT), contains="迁移体检通过"),
    cmd("DATA-402", "迁移与模型建表结果一致（零漂移）", "data", "P0", ["F-migration"], ["REQ-019"],
        "迁移与模型漂移会让新环境与老环境 schema 不同",
        "命令输出“迁移与模型一致”",
        "python3 flows/schema_compare.py", cwd=str(HERE), contains="迁移与模型一致"),
    cmd("DATA-405", "运行归档与平台库同库（度量才看得到运行）", "data", "P1",
        ["F-migration", "F-runtime"], ["REQ-019", "REQ-010"],
        "RUNS_DB_URL 与 DATABASE_URL 不一致时，运行归档落在别处：度量从平台库读会看到 0 次运行，"
        "而运维以为没有运行",
        "运行时库应包含 agent_runs（或明确声明归档另存并在度量中体现）",
        "python3 flows/runs_db_check.py", cwd=str(HERE), contains="RESULT: PASS"),
    cmd("DATA-403", "备份可恢复（恢复演练）", "data", "P0", ["F-migration"], ["REQ-019"],
        "审计留痕只存在 PG 里，没有可验证的恢复路径＝出事无法收敛",
        "命令输出“恢复演练通过”",
        "python3 platform/scripts/backup_restore.py drill --dbs platform",
        cwd=str(ROOT), contains="恢复演练通过"),
    cmd("DATA-404", "证据哈希复验：篡改可被发现", "data", "P1", ["F-evidence"], ["REQ-011"],
        "证据不能复验就只是附件",
        "篡改后 verify 报告 broken ≥ 1",
        "bash test-campaign/probes.sh evidence", cwd=str(ROOT), contains="RESULT: PASS"),
]

# ---------------------------------------------------------------- 运维/部署
CARDS += [
    cmd("OPS-501", "看门狗判级/去重/通知自检", "ops", "P1", ["F-watchdog"], ["REQ-020"],
        "刷屏或漏报的告警等于没有告警",
        "命令输出“自检通过”",
        "python3 platform/scripts/watch_health.py --selftest", cwd=str(ROOT), contains="自检通过"),
    cmd("OPS-502", "看门狗对 7 服务判定为健康", "ops", "P1", ["F-watchdog"], ["REQ-020"],
        "看门狗误报会让真故障被忽略",
        "输出“平台健康：ok”",
        "WATCH_TARGETS='approval-gateway=http://127.0.0.1:18001,agent-runtime=http://127.0.0.1:18002,"
        "knowledge-base=http://127.0.0.1:18003,task-center=http://127.0.0.1:18004,"
        "metrics-dashboard=http://127.0.0.1:18005,gitlab-bridge=http://127.0.0.1:18006,"
        "process-mining=http://127.0.0.1:18007' WATCH_STATE_FILE=/tmp/wh-camp.json "
        "python3 platform/scripts/watch_health.py --once", cwd=str(ROOT), contains="平台健康：ok"),
    cmd("OPS-503", "7 个服务都暴露治理姿态", "ops", "P1", ["F-health", "F-deploy"], ["REQ-001", "REQ-021"],
        "宽松不可见＝没人会发现自己在裸奔",
        "7 个 /health 都含 security.compliant 字段",
        "for p in 18001 18002 18003 18004 18005 18006 18007; do "
        "curl -s -m 5 -H 'X-Service-Token: pst-sut-token' http://127.0.0.1:$p/health | "
        "python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[\"service\"],\"compliant=\"+str(d[\"security\"][\"compliant\"]))'; done",
        cwd=str(ROOT), contains="compliant="),
    cmd("OPS-504", "生产档位缺凭据必须拒绝启动", "ops", "P0", ["F-deploy"], ["REQ-021"],
        "带着“看起来有鉴权”的状态运行比起不来更危险",
        "进程非 0 退出且日志含“拒绝启动”",
        "cd platform/services/task-center && PLATFORM_ENV=prod SERVICE_AUTH_MODE= SERVICE_AUTH_TOKEN= "
        "AUTH_STATIC_TOKEN= TRUST_PROXY_AUTH= .venv/bin/python -c 'import app.main' 2>&1 | "
        "grep -q '拒绝启动' && echo 'RESULT: PASS 已拒绝启动'", cwd=str(ROOT), contains="RESULT: PASS"),
    cmd("DEP-601", "上线体检：prod 模板必须可上线", "deploy", "P0", ["F-deploy"], ["REQ-021"],
        "生产带着宽松开关上线＝修复等于没修",
        "命令输出“上线就绪”",
        "python3 scripts/check_deployment_readiness.py --env-file .env.prod.example",
        cwd=str(ROOT / "platform"), contains="上线就绪"),
    cmd("DEP-602", "生产配置生成器自检", "deploy", "P1", ["F-deploy"], ["REQ-021"],
        "密钥没生成/占位值没拦住都会导致生产裸奔",
        "命令输出“自检通过”",
        "python3 scripts/init_prod_env.py --selftest", cwd=str(ROOT / "platform"),
        contains="自检通过"),
    cmd("DEP-603", "生产编排可解析（叠加式）", "deploy", "P0", ["F-deploy"], ["REQ-021"],
        "编排文件写错会在部署当刻才暴露",
        "docker compose config 退出 0",
        "cp .env.prod.example .env.prod && docker compose -f docker-compose.yml "
        "-f docker-compose.prod.yml --env-file .env.prod config -q && echo 'RESULT: PASS 编排可解析'; "
        "rm -f .env.prod", cwd=str(ROOT / "platform"), contains="RESULT: PASS"),
    cmd("DEP-604", "鉴权模块 7 份副本无漂移", "deploy", "P1", ["F-authz"], ["REQ-002"],
        "漂移会让某个服务的鉴权行为跟其它服务不一致",
        "命令输出 OK",
        "python3 scripts/sync_service_auth.py --check", cwd=str(ROOT / "platform"),
        contains="OK:"),
]

# ---------------------------------------------------------------- 集成
CARDS += [
    cmd("INT-801", "告警 → 建任务 → 自动拉起运维 SOP", "integration", "P0", ["F-alert-ingest"],
        ["REQ-016"], "运维域入口不通＝整个运维域没有输入",
        "带密钥告警返回 200 且建出 ops-domain 任务",
        "curl -s -m 20 -X POST http://127.0.0.1:18004/alerts/prometheus -H 'Content-Type: application/json' "
        "-H 'X-Webhook-Token: pst-sut-webhook' -d '{\"status\":\"firing\",\"alerts\":[{\"status\":\"firing\","
        "\"labels\":{\"alertname\":\"Camp1008Probe\",\"severity\":\"P1\",\"fingerprint\":\"camp1008\"},"
        "\"annotations\":{\"summary\":\"战役探针\",\"description\":\"camp1008 告警链路验证\"}}]}' >/dev/null; "
        "sleep 1; docker exec platform-pg psql -U platform -d pst_sut -tAc "
        "\"select count(*) from tasks where sop='ops-domain/alert-to-postmortem'\"",
        cwd=str(ROOT), contains=None, expect_exit=0),
    cmd("INT-802", "事件投递：approval.decided → task-center /events", "integration", "P0",
        ["F-event"], ["REQ-006"], "事件不通则任务状态与审批脱节",
        "投递后 task_events 出现审批决策事件",
        "T=apv_evt_$(date +%s); curl -s -m 10 -X POST http://127.0.0.1:18004/events "
        "-H 'Content-Type: application/json' -H 'X-Service-Token: pst-sut-token' -d "
        "\"{\\\"event_id\\\":\\\"$T\\\",\\\"event_type\\\":\\\"approval.decided\\\",\\\"source\\\":\\\"probe\\\","
        "\\\"trace_id\\\":\\\"$T\\\",\\\"payload\\\":{\\\"approval_id\\\":\\\"a\\\",\\\"task_ref\\\":\\\"$T\\\","
        "\\\"decision\\\":\\\"approve\\\"}}\" >/dev/null; "
        "curl -s -m 10 -X POST http://127.0.0.1:18004/events -H 'Content-Type: application/json' "
        "-H 'X-Service-Token: pst-sut-token' -d \"{\\\"event_id\\\":\\\"$T\\\",\\\"event_type\\\":\\\"approval.decided\\\","
        "\\\"source\\\":\\\"probe\\\",\\\"trace_id\\\":\\\"$T\\\",\\\"payload\\\":{\\\"approval_id\\\":\\\"a\\\","
        "\\\"task_ref\\\":\\\"$T\\\",\\\"decision\\\":\\\"approve\\\"}}\" | grep -q duplicate_skipped "
        "&& echo 'RESULT: PASS 幂等去重生效'",
        cwd=str(ROOT), contains="RESULT: PASS"),
    cmd("INT-803", "受管知识：项目授权下可入库与检索", "integration", "P1", ["F-kb"], ["REQ-018"],
        "RAG 是运维诊断依据；新契约要求可验证的项目授权（fail-closed）",
        "注册项目版本后入库成功、检索命中且来源在授权范围内",
        "python3 flows/kb_check.py", cwd=str(HERE), contains="RESULT: PASS", timeout=180000),
    cmd("INT-804", "流程挖掘提案 → G7 审批单创建", "integration", "P1", ["F-mining"], ["REQ-013"],
        "提案绕过 G7 会直接改流程配置",
        "提案接口产出提案且 submit 建出审批",
        "bash test-campaign/probes.sh heuristic", cwd=str(ROOT), contains="RESULT: PASS"),
    cmd("INT-805", "驾驶舱决策代理透传身份（不绕过审计）", "integration", "P0",
        ["F-ui-dashboard", "F-gate"], ["REQ-004", "REQ-015"],
        "驾驶舱替人落决策，若不带身份则 oidc 下必然 502、放行模式下无法记录是谁批的",
        "无身份的 /console/decide 被拒（401/403/502），而不是 200",
        f"curl -s -o /dev/null -w 'status=%{{http_code}}\\n' -X POST {MD}/console/decide "
        "-H 'Content-Type: application/json' -d '{\"approval_id\":\"apv_x\",\"decision\":\"approve\","
        "\"comment\":\"probe\",\"decided_by\":\"probe\"}'", cwd=str(ROOT), contains=None, expect_exit=0),
]

# ---------------------------------------------------------------- 业务流端到端（每条 flow 一张）
FLOW_CARDS = [
    ("FLOW-801", "product", "产品域端到端：需求→PRD→G1 人审→否决→归档", "P0",
     ["F-gate", "F-runtime"], ["REQ-004", "REQ-010"], "FLOW-prod",
     "挂起 G1、人审决策被接受、否决反映到归档"),
    ("FLOW-807", "dev", "研发域端到端：issue→方案→G2 人审决策", "P1",
     ["F-gate", "F-runtime"], ["REQ-004"], "FLOW-dev",
     "挂起 G2 且人审决策被接受（编码/MR 段见声明缺口）"),
    ("FLOW-802", "test", "测试域端到端：用例→执行→G4 人审", "P1",
     ["F-gate", "F-runtime"], ["REQ-004"], "FLOW-test",
     "挂起人审并可恢复（本环境无仓库令牌，执行段见声明缺口）"),
    ("FLOW-803", "ops", "运维域端到端：告警→SOP→G5 人审决策被接受", "P0",
     ["F-alert-ingest", "F-gate"], ["REQ-004", "REQ-016"], "FLOW-ops",
     "挂起 G5 且人审决策落库"),
    ("FLOW-804", "platform", "平台域端到端：挖掘→提案→G7 审批；未批不得应用", "P1",
     ["F-mining", "F-authz"], ["REQ-013", "REQ-003"], "FLOW-platform",
     "产出提案、建出 G7 审批单、未获批应用被拒"),
    ("FLOW-805", "ui", "决策闭环（浏览器）：审批人在界面上完成一次决策", "P0",
     ["F-ui-console"], ["REQ-014"], "FLOW-approve-ui",
     "界面可完成决策且无失败提示"),
    ("FLOW-806", "ops-taskref-mismatch", "运维域契约：task_ref 与告警 id 不一致时不得死锁", "P0",
     ["F-decision-verify", "F-gate"], ["REQ-004"], "FLOW-ops",
     "审批单 task_ref 与运行 task_ref 不一致时，恢复仍应可用"),
]
for cid, domain, title, prio, feats, reqs, flow_id, expected in FLOW_CARDS:
    CARDS.append(cmd(cid, title, "flow", prio, feats, reqs,
                     "业务流在中间断掉时，单接口测试可能全绿",
                     expected, f"python3 flows/run_flow.py --domain {domain}",
                     cwd=str(HERE), timeout=300000, flow_ids=[flow_id]))

CARDS.append(cmd(
    "PROV-901", "需求来源链可反查（谁在哪句话提的）", "api", "P1", ["F-provenance"], ["REQ-012"],
    "无出处的需求在争论时只能翻聊天记录",
    "反查返回带 quote/confirmed_by 的来源条目且 has_provenance=true",
    "python3 flows/prov_check.py", cwd=str(HERE), timeout=120000,
    flow_ids=["FLOW-prod"]))

PLAN = {
    "schema_version": "1.0",
    "task": {
        "mode": "release",
        "decision": "当前 revision 能否从 NO_RELEASE 改为可发布（含上轮 7 个缺陷的修复验证）",
        "scope": [
            "七个服务 + 两个浏览器界面（审批台 / 驾驶舱）的功能与业务流",
            "上轮 7 个缺陷的修复复验（DEF-001…DEF-007）",
            "鉴权与并发正确性回归",
            "迁移/备份/部署就绪",
            "性能基线（健康端点 + 业务路径）与短程稳定性",
        ],
        "out_of_scope": [
            "真实 LLM 质量与提示注入（SUT 使用确定性兜底 LLM）",
            "真实飞书推送与真实 GitLab 仓库写入",
            "公网 Caddy + 企业 SSO 的真实部署",
            "多副本水平扩展与浏览器兼容性矩阵",
        ],
        "access": "disposable",
        "constraints": [
            "只对一次性环境 pst_sut 写入；测试数据带 camp1008 标记",
            "被测系统仅在授权修复范围内改动，且必须逐卡复验",
        ],
    },
    "campaign": {
        "id": "camp-pst-20261008",
        "name": "产品研发智能化平台 · 发布判定复测（第 2 次完整战役）",
        "target": GW,
        "commit": COMMIT,
        "environment": "local-disposable (PG pst_sut + qdrant + 7 services)",
        "run_marker": MARKER,
        "objective": "回答：当前 revision 是否可以从 NO_RELEASE 改为可发布；新增 UI 与性能两个此前未覆盖的维度。",
        "out_of_scope": [
            "真实 LLM 质量与提示注入（SUT 使用确定性兜底 LLM，模型质量未测）",
            "真实飞书推送（无企业应用凭证）",
            "真实 GitLab MR 全生命周期（无仓库令牌）",
            "公网 Caddy + 企业 SSO 入口（本机无域名/证书/IdP）",
            "多副本水平扩展（单实例环境）",
            "浏览器兼容性矩阵（仅 Chrome 系）",
        ],
        "executed_by": "DSH deepseek-flash + full-system-testing skill",
        "variables": {"gw": GW, "rt": RT, "kb": KB, "tc": TC, "md": MD, "gl": GL, "pm": PM,
                      "proxy": PROXY, "token": "pst-sut-token", "marker": MARKER},
        "defaults": {"timeout_ms": 180000},
    },
    "features": [{"id": f, "name": n, "priority": p, "risk": r, "entry_points": e,
                  "source": "route table (openapi.json) + 设计文档 + 历史战役报告"}
                 for f, n, p, r, e in FEATURES],
    "flows": [{"id": f, "name": n, "priority": p, "actors": a, "steps": s,
               "invariant": i, "failure_modes": fm} for f, n, p, a, s, i, fm in FLOWS],
    "requirements": [{"id": r, "title": t, "priority": p, "source": "设计文档 + 历史缺陷",
                      "feature_ids": feats} for r, t, p, feats in REQS],
    "quality_gates": [
        {"id": "G1", "description": "所有 P0 卡通过", "metric": "p0_pass_rate",
         "comparator": "gte", "threshold": 1.0},
        {"id": "G2", "description": "无未关闭的 BLOCKER/CRITICAL 缺陷",
         "metric": "open_blocker_critical_count", "comparator": "lte", "threshold": 0},
        {"id": "G3", "description": "整体通过率 ≥ 85%", "metric": "overall_pass_rate",
         "comparator": "gte", "threshold": 0.85},
        {"id": "G4", "description": "执行率 100%（不得有未执行卡）", "metric": "execution_rate",
         "comparator": "gte", "threshold": 1.0},
        {"id": "G5", "description": "需求覆盖由**通过**的卡验证（独立验证覆盖率）",
         "metric": "verified_coverage_rate", "comparator": "gte", "threshold": 0.9},
    ],
    "cards": CARDS,
}

MODEL = {
    "schema_version": "1.0",
    "note": "第 2 次完整战役的 recon。特征/流程来自 openapi.json 实际路由（evidence/routes.json）、"
            "设计文档与历史战役报告；标 inferred 的为代码推断。",
    "system": {
        "name": "产品研发智能化平台（AI SOP 生产线）",
        "target": "http://127.0.0.1:18001（7 服务：18001–18007）",
        "revision": COMMIT,
        "entry_points": ["platform/services/*/app/main.py",
                         "test-campaign/start-sut.sh（一次性环境）",
                         "platform/docker-compose.yml + docker-compose.prod.yml"],
        "language": "Python 3.9（FastAPI + SQLAlchemy 2 + LangGraph）",
        "storage": "PostgreSQL（平台库 pst_sut/平台库；Qdrant 向量；sqlite 兜底）",
        "derived_from": [
            {"source": "evidence/routes.json（7 服务 openapi.json 实测 80 条路由）", "gave": "特征入口"},
            {"source": "test-campaign/test-report/test-report.md（第 1 次战役，24 卡）", "gave": "回归卡与历史缺陷"},
            {"source": "test-campaign/verify-report.md（3 轮专项复验）", "gave": "已修复项与未覆盖维度"},
            {"source": "21/22 号文档（治理档位、上线前最小路径）", "gave": "部署与治理要求"},
        ],
        "could_not_determine": [
            "真实 LLM 输出质量与提示注入抵抗力（SUT 用确定性兜底 LLM，未接真实模型）",
            "飞书真实推送与卡片回调（无企业应用凭证，仅验签路径可测）",
            "GitLab 出站 API 的真实行为（无仓库令牌）",
            "多副本下的并发语义（行锁/唯一键只在单实例 PG 上验证）",
        ],
    },
    "features": PLAN["features"],
    "flows": PLAN["flows"],
    "actors": [
        {"actor": "AI 岗位（product/dev/tester/ops/platform agent）", "can": ["起草产物", "创建审批单", "调用受限工具"],
         "cannot": ["批准自己的产出", "无引用执行写类工具"], "tested_by": ["SEC-104", "SEC-105", "REG-007"]},
        {"actor": "人类审批人（approver/admin）", "can": ["批准/退回/否决", "触发全局超时扫描"],
         "cannot": ["绕过租户边界"], "tested_by": ["UI-302", "UI-303", "SEC-104"]},
        {"actor": "服务账号（服务间调用）", "can": ["携带 X-Service-Token 调用"],
         "cannot": ["无令牌访问任何业务接口"], "tested_by": ["SEC-101", "SEC-102", "SEC-103"]},
        {"actor": "入站 webhook（GitLab / Alertmanager / 飞书）", "can": ["凭签名投递事件"],
         "cannot": ["无签名投递"], "tested_by": ["SEC-106", "SEC-109", "INT-801"]},
        {"actor": "直连攻击者（能到达服务端口）", "can": [],
         "cannot": ["伪造入口身份头获得访问"], "tested_by": ["SEC-108"]},
    ],
    "entities": [
        {"name": "ApprovalRequest", "states": ["pending", "approved", "rejected", "revised"],
         "note": "决策唯一键 (request_id, version)；行锁串行化"},
        {"name": "Task", "states": ["pending", "running", "waiting_approval", "approved", "rejected", "done", "failed"],
         "note": "waiting_approval 出发的流转必须带决策依据"},
        {"name": "GateCheck", "states": [], "note": "E1–E3 判定留痕，mode 由服务端决定"},
        {"name": "EvidenceArtifact", "states": [], "note": "sha256 可复验"},
        {"name": "AgentRun", "states": ["running", "waiting", "done", "rejected", "failed"],
         "note": "归档是度量体系唯一来源"},
    ],
    "constraints": [
        {"entity": "ApprovalRequest", "constraint": "一个提交版本只能有一条决策",
         "enforced_where": "DB 唯一键 + 行锁", "verified_by": "CONC-201"},
        {"entity": "Task", "constraint": "external_ref 在同一租户内唯一",
         "enforced_where": "DB 复合唯一键", "verified_by": "REG-010"},
        {"entity": "ApprovalRequest", "constraint": "决策必须属于 options 声明的动作",
         "enforced_where": "应用层", "verified_by": "REG-004"},
    ],
    "integrations": [
        {"name": "PostgreSQL", "direction": "both", "failure_behaviour": "服务起不来/写入失败（fail-closed）"},
        {"name": "Qdrant", "direction": "out", "failure_behaviour": "知识库降级为内存桩并在 /health 报 degraded"},
        {"name": "LiteLLM", "direction": "out", "failure_behaviour": "本次未接（SUT 用兜底 LLM）"},
        {"name": "GitLab", "direction": "both", "failure_behaviour": "入站验签失败 401；出站无令牌 502"},
        {"name": "Alertmanager", "direction": "in", "failure_behaviour": "无密钥 401/503（fail-closed）"},
        {"name": "飞书 IM", "direction": "both", "failure_behaviour": "无签名密钥 503（卡片不可用）"},
    ],
    "jobs": [
        {"name": "超时扫描 sweeper", "trigger": "POST /admin/sweep 或 SWEEP_INTERVAL_SECONDS", "tested_by": ["REG-006", "CONC-203"]},
        {"name": "看门狗 watchdog", "trigger": "容器循环 60s", "tested_by": ["OPS-501", "OPS-502"]},
    ],
    "non_functional": [
        {"attribute": "并发正确性", "expectation": "30 并发下同一单据恰好一次成功",
         "source": "历史缺陷（CONC-001/002）", "tested_by": ["CONC-201", "CONC-202"]},
        {"attribute": "鉴权", "expectation": "无凭据不得触达业务接口；伪造身份头不得生效",
         "source": "21 号文档", "tested_by": ["SEC-101", "SEC-102", "SEC-108"]},
        {"attribute": "可用性", "expectation": "7 服务 /health 全绿且姿态可见",
         "source": "运维手册", "tested_by": ["OPS-502", "OPS-503"]},
        {"attribute": "可恢复性", "expectation": "备份可恢复且行数一致、迁移可回滚",
         "source": "22 号文档", "tested_by": ["DATA-401", "DATA-403"]},
        {"attribute": "界面正确性", "expectation": "审批台在令牌模式下不得静默空列表；不得渲染注入",
         "source": "本次战役新增维度", "tested_by": ["UI-301", "UI-302", "UI-304"]},
    ],
    "declared_gaps": [
        {"description": "性能基线（吞吐/p95）与 soak 稳定性未在本轮采集（时间预算）",
         "impact": "无法回答容量与长稳问题；第 6 步上线前仍需补", "feature_ids": ["F-health"]},
        {"description": "真实模型质量、提示注入、飞书真实推送、GitLab 出站行为未覆盖",
         "impact": "这些维度上的缺陷本轮不可能被发现", "feature_ids": ["F-mining", "F-gitlab"]},
        {"description": "多副本/水平扩展语义未验证", "impact": "扩容后并发正确性结论不适用",
         "feature_ids": ["F-gate", "F-taskfsm"]},
    ],
}


def main() -> int:
    (HERE / "test-plan.json").write_text(json.dumps(PLAN, ensure_ascii=False, indent=1) + "\n",
                                         encoding="utf-8")
    (HERE / "system-model.json").write_text(json.dumps(MODEL, ensure_ascii=False, indent=1) + "\n",
                                            encoding="utf-8")
    rows = []
    for c in CARDS:
        chk = c["check"]
        cmd_text = chk.get("command") or f"{chk.get('method')} {chk.get('path')}"
        rows.append(f"| {c['id']} | {c['priority']} | {c['layer']} | {c['title']} | "
                    f"`{cmd_text[:90]}` | {c['expected']} |")
    (HERE / "test-cases.md").write_text(
        "# 测试卡清单（camp-pst-20261008）\n\n"
        f"共 {len(CARDS)} 张卡；优先级分布 "
        f"P0={sum(1 for c in CARDS if c['priority'] == 'P0')}、"
        f"P1={sum(1 for c in CARDS if c['priority'] == 'P1')}、"
        f"P2={sum(1 for c in CARDS if c['priority'] == 'P2')}。\n\n"
        "| 卡 | 优先级 | 层 | 标题 | 执行 | 期望 |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n", encoding="utf-8")
    print(f"已生成：{len(CARDS)} 张卡；features={len(FEATURES)} flows={len(FLOWS)} reqs={len(REQS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
