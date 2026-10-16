# 缺陷台账（camp-pst-20261008 · 修复复验轮）

共 10 条：{'CRITICAL': 2, 'MAJOR': 3, 'MINOR': 5}；状态：{'RETEST_PASS': 6, 'OPEN': 4}。
卡片：71 张 100% 执行，70 通过 / 1 失败（环境限制）。

> 本轮为**授权修复后的复验**：上轮缺陷用原失败卡复跑验证（RETEST_PASS），原始失败记录保留在 `before-fix/`。

## DEF-001 · 审批台在启用鉴权的部署下不可用，且把 401 呈现为“暂无待审批事项”

- **严重度**：CRITICAL　**状态**：RETEST_PASS　**相关卡**：UI-301, UI-302, UI-303, UI-304, UI-305, UI-308, UI-309, FLOW-805
- **根因**：控制台前端 app.js 不携带任何凭据（其设计里也没有登录态）：页面本身作为静态资源被豁免，但它调用的 GET /approvals 在 SERVICE_AUTH_MODE=token 下要求服务令牌，POST /approvals/{id}/decision 还额外要求 Bearer（AUTH_MODE=static/oidc）。前端把 fetch 的 401 当成“空列表”渲染，于是审批人看到“暂无待审批事项 🎉”。
- **影响**：人审决策界面在任何启用鉴权的部署下不可用（只有 AUTH_MODE=off 可用），且以“无事可做”的假象呈现——闸门停摆而无人察觉。直接冲击平台的核心承诺（AI 执行、人在闸门决策）。
- **复现**：① 库里有 11 条待审：`docker exec platform-pg psql -U platform -d pst_sut -tAc "select count(*) from approval_requests where status='pending'"`；② 页面实际调用的接口：`curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:18001/approvals?status=pending'` → 401；③ 浏览器打开 http://127.0.0.1:18001/ → 列表显示“暂无待审批事项”（截图 evidence/ui/screenshots/UI-301-FAIL-*.png），控制台报 401；④ 受控实验（AUTH_MODE=static + TRUST_PROXY_AUTH=1，模拟文档所述入口拓扑）：只带 X-Platform-User 时 GET /approvals=200 但 POST …/decision=401，带 Bearer 才 200。
- **建议修法**：给控制台一条明确的凭据路径（入口代理同时注入服务令牌与 X-Platform-User，或由 BFF 代持令牌），并且前端必须把 401/403 显示为错误状态而不是空列表；补一条 E2E 卡：登录态下必须能看到待办并完成一次决策。
- **复验**：修复前 OPEN；本轮 FIXED。控制台改为页面内输入个人令牌（Bearer）、显式展示 401/403/网络错误、失败不再伪装空队列；本轮浏览器卡全部通过（含界面完成决策）。

## DEF-002 · process-mining 调审批网关不带服务令牌：平台域（SOP 自进化）整链不可用

- **严重度**：CRITICAL　**状态**：RETEST_PASS　**相关卡**：FLOW-804
- **根因**：platform/services/process-mining/app/main.py 的两处出站调用（提交 G7 审批 ~L175、读审批状态 ~L188）缺少 headers=outbound_headers()——上一轮鉴权专项的批量修改因为正则无法跨越 rstrip('/') 里的括号而漏掉了它们，单测用 mock 也没暴露。
- **影响**：SERVICE_AUTH_MODE=token 的部署里，流程挖掘提案无法创建 G7 审批单 → “瓶颈→提案→人审→应用”整条平台域链路不可用，且错误以 502 形式出现，调用方看不出是缺令牌。
- **复现**：`python3 flows/run_flow.py --domain platform` → `RESULT: FAIL 提交 G7 审批失败 HTTP 502: 创建 G7 审批失败: Client error '401 Unauthorized' for url 'http://127.0.0.1:18001/approvals'`；对照：`grep -n 'outbound_headers' platform/services/process-mining/app/main.py` 只有 import。
- **建议修法**：两处补 headers=outbound_headers()；并加一条集成卡：token 模式下每个服务都能创建审批单。
- **复验**：修复前 OPEN；本轮 FIXED。process-mining 出站调用已带服务凭据；挖掘→提案→G7 本轮 PASS。

## DEF-003 · 直连服务伪造 X-Platform-User 身份头被接受（缺少纵深防御）

- **严重度**：MAJOR　**状态**：RETEST_PASS　**相关卡**：SEC-108
- **根因**：TRUST_PROXY_AUTH=1 时服务无条件信任 X-Platform-User，唯一防线是“入口会剥离同名头”这一部署假设；服务侧没有任何共享密钥能区分“入口注入”与“直连伪造”。
- **影响**：任何能到达服务端口的人可冒充已登录用户（含审批人）读取驾驶舱数据；一旦某个部署把服务端口发布到宿主机或攻击者进入内网，身份体系即失效。
- **复现**：`curl -s -o /dev/null -w '%{http_code}' -H 'X-Platform-User: attacker' http://127.0.0.1:18005/` → 200（无任何密钥）。
- **建议修法**：入口在注入 X-Platform-User 的同时注入服务令牌（只有入口与平台知道），或引入独立的代理共享密钥头；并在 Caddyfile 注释里写明该头必须剥离。
- **复验**：修复前 OPEN；本轮 FIXED。可信代理改为独立代理密钥（X-Proxy-Token + PROXY_AUTH_TOKEN）；伪造 X-Platform-User 直连得 401。

## DEF-004 · 运维域 G5 决策核验死锁：审批单 task_ref 与运行 task_ref 不一致时永远批不下来

- **严重度**：MAJOR　**状态**：RETEST_PASS　**相关卡**：FLOW-806
- **根因**：ops SOP 建 G5 审批单时用 alert.id 作为 task_ref（sop.py ~L732），而运行状态的 task_ref 是调用方另传的字段；决策核验（gate_decisions.verify）比较这两个值，不一致即 409，且没有任何路径能修复该运行。生产路径恰好让两者相等，故此前未被发现。
- **影响**：任何直接调用 /runs 启动运维域 SOP 且 task_ref≠alert.id 的调用方，其 G5 永远无法批准，只能否决重来；错误信息（“不是同一件事”）也无法指向真正原因。
- **复现**：`python3 flows/run_flow.py --domain ops-taskref-mismatch` → 409：“审批 apv_… 属于 'camp1008-alert'，与本次运行 'camp1008-flow-ops' 不是同一件事”。
- **建议修法**：让审批与运行共用同一 task_ref（单一事实来源），或在核验时按域归一（ops 用 alert.id）。
- **复验**：修复前 OPEN；本轮 FIXED。G5/G6 优先使用运行的 task_ref；task_ref 与告警 id 不一致时不再死锁。

## DEF-005 · 外部依赖失败时 /runs 返回裸 500，调用方无法区分“环境缺配置”与“平台缺陷”

- **严重度**：MINOR　**状态**：RETEST_PASS　**相关卡**：FLOW-802
- **根因**：agent-runtime 的 SOP 节点直接抛出底层异常（GitLab 502、git checkout 失败等），未映射为带依赖名的 502/503 与结构化 detail。
- **影响**：运维无法从响应判断该找谁（改配置还是修代码）；本次战役的测试域卡因此无法自动判级，只能人工归因为环境限制。
- **复现**：`python3 flows/run_flow.py --domain test` → HTTP 500 {'detail': 'Internal Server Error'}（日志里才是 CodingError）。
- **建议修法**：把集成失败映射为 502/503 + {dependency, operation, hint}；运行仍应归档为 failed。
- **复验**：修复前 OPEN；本轮 FIXED。外部依赖失败改为结构化 502（error/kind/thread_id）；测试域仍因无仓库凭证失败，但错误已可诊断。

## DEF-006 · 审批单顶层 sources 被静默忽略：审计输入丢失且无任何提示

- **严重度**：MINOR　**状态**：RETEST_PASS　**相关卡**：PROV-901
- **根因**：请求模型未禁止未知字段（pydantic 默认 extra=ignore）；sources 属于 context_snapshot.sources。
- **影响**：调用方以为记录了需求出处，实际得到 201 + 空来源链；问题延迟到 E1 闸门报“缺少业务来源”时才暴露，属于审计追溯链的静默缺失。
- **复现**：POST /approvals 顶层带 sources → HTTP 201 且返回 context_snapshot.sources=[]；同一请求把 sources 放进 context_snapshot → 来源链反查命中。
- **建议修法**：请求模型设 model_config={'extra':'forbid'}（或显式接受并合并顶层 sources）。
- **复验**：修复前 OPEN；本轮 FIXED。来源链反查本轮通过；新代码对项目版本等输入采用 extra=forbid。

## DEF-007 · 运行归档库可与度量库不一致且无任何检查：度量看到 0 次运行

- **严重度**：MINOR　**状态**：OPEN　**相关卡**：DATA-405
- **根因**：agent-runtime 归档库由 RUNS_DB_URL 优先决定、其次 DATABASE_URL；两者不一致时运行记录静默落到另一个库，而驾驶舱/度量从平台库读 agent_runs。
- **影响**：运维在驾驶舱看到“没有运行”，而实际有运行——典型的静默错误；本次战役环境（RUNS_DB_URL 指向 sqlite）就出现了平台库没有 agent_runs 表。
- **复现**：`python3 flows/runs_db_check.py` → 平台库无 agent_runs 表；`GET /runs` 却能看到运行。
- **建议修法**：/health 报告归档库并在与度量库不一致时告警；部署文档明确 RUNS_DB_URL 不得另指。
- **复验**：修复前 OPEN；本轮 OPEN。健康检查已能区分实际归档库；本 SUT 仍把归档指向独立库，度量看不到运行（部署一致性待收紧）。

## DEF-008 · 同时携带服务令牌与人类 Bearer 的请求被判 401：多跳人工链路整体失效

- **严重度**：MAJOR　**状态**：OPEN　**相关卡**：—
- **根因**：中间件先按服务令牌放行，随后见到 Authorization: Bearer 就要求其可验证为个人 JWT，失败即整请求 401；而 outbound_headers() 同时透传调用方 Bearer 并补服务令牌。
- **影响**：驾驶舱点批准实测 502（网关 401）；人类凭据调 /resume 时内部回查网关 401 → 运行无法恢复（409）。平台自身 resume 用服务凭据可绕过。
- **复现**：① 经代理 POST /console/decide → 502（体内网关 401）；② 人类 Bearer 调 POST /runs/<id>/resume → 409；同调用改服务令牌 → 200。
- **建议修法**：出站二选一；中间件把'服务身份已通过'与'Bearer 不可验证'分开处理。


## DEF-009 · 审批列表接口未分页：10 并发下 p95 623.9ms 并随待办量劣化

- **严重度**：MINOR　**状态**：OPEN　**相关卡**：—
- **根因**：GET /approvals 一次返回全部待审单据并拼装三要素，无分页或上限参数。
- **影响**：待办 40 条时 10 并发 p95 已达 623.9ms（实测 SLO 400ms 未达）；待办增长会继续拖慢审批台首屏。
- **复现**：loadtest --url http://127.0.0.1:18021 --endpoint 'GET /approvals' --scenario load --vus 10 --duration 20 --slo-p95 400 → p95=623.9ms / 22.96 rps / 0 错误。
- **建议修法**：加 limit/offset 与默认上限；控制台分页或按需加载详情。


## DEF-010 · 审批台用原生 prompt() 收集决策人姓名：自动化受阻且与服务端身份重复

- **严重度**：MINOR　**状态**：OPEN　**相关卡**：UI-309
- **根因**：app.js 的 decide() 先 window.prompt 取姓名再提交；原生弹窗会挂住无头驱动。
- **影响**：界面自动化必须替换 prompt 才能完成决策；真实用户多一次无谓输入。
- **复现**：browser_test.py --suite ui/console-suite.json --only UI-309（不替换 prompt 时 BLOCKED）。
- **建议修法**：改为页面内输入框或直接使用服务端身份。

