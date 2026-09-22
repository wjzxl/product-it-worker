# 缺陷修复复验报告（camp-pst-20260922 后续）

> 复验时间：2026-09-22T10:20:38.709612+00:00
> 方法：**逐卡重跑原探针**，不改动断言（唯一例外见第 5 节）。原报告 `test-report/test-report.md` 保持不变，
> 本文件与其**并列**，记录修复后的新结果。

## 1. 结果总览

| 指标 | 修复前（原报告） | 修复后（本次复验） |
|---|---|---|
| 测试卡 | 24 张，通过 9（37.5%） | 失败卡 **15/15 + 原有通过卡 3/3 = 18/18 PASS** |
| FAIL 卡 | 15 | **0** |
| 未关闭 BLOCKER/CRITICAL | 7 | **0**（全部修复并逐卡复验） |
| 全平台单测 | 401 | **454 全绿**（+41 防复发用例） |

## 2. 逐卡复验（旧 → 新）

| 卡 | 缺陷 | 复验探针/命令 | 修复前 | 修复后 |
|---|---|---|---|---|
| SEC-001 | BUG-001 | `im-callback` | FAIL | **PASS** |
| SEC-003 | BUG-002 | `resubmit-tenant` | FAIL | **PASS** |
| SEC-004 | BUG-003 | `gate-mode` | FAIL | **PASS** |
| GATE-001 | BUG-014 | `gate-persist` | FAIL | **PASS** |
| GATE-002 | BUG-004 | `timeout-auto` | FAIL | **PASS** |
| GATE-003 | BUG-013 | `GATE-003` | FAIL | **PASS** |
| FSM-001 | BUG-005 | `task-fsm` | FAIL | **PASS** |
| FSM-003 | BUG-006 | `task-tenant` | FAIL | **PASS** |
| FSM-004 | BUG-015 | `task-dup-ref` | FAIL | **PASS** |
| RT-001 | BUG-012 | `reject-archive` | FAIL | **PASS** |
| RT-002 | BUG-007 | `wake-guard` | FAIL | **PASS** |
| ALG-001 | BUG-008 | `heuristic` | FAIL | **PASS** |
| ALG-002 | BUG-009 | `alpha` | FAIL | **PASS** |
| ALG-003 | BUG-010 | `dfg-conformance` | FAIL | **PASS** |
| ALG-004 | BUG-011 | `llm-fallback` | FAIL | **PASS** |

## 3. 根因与修法

| 缺陷 | 根因 | 修法 | 文件 |
|---|---|---|---|
| BUG-001 | IM 回调无签名、无鉴权、无租户校验（源码留有 TODO） | 新增飞书签名校验（sha256(ts+nonce+key+body)、时间窗防重放、compare_digest）；未配置密钥即拒绝(503)，本地演示需显式 IM_CALLBACK_ALLOW_UNSIGNED=1；补租户过滤 | `app/im_auth.py`(新)、`app/router.py` |
| BUG-002 | resubmit 的 _get_or_404 未传 tenant | 与兄弟路由一致传租户 | `app/router.py` |
| BUG-003/014 | 请求体 mode/persist 可覆盖服务端规则 | 端点显式忽略客户端 mode/persist（并告警）；宽严只由规则文件+ENG_GATES_MODE 决定，留痕强制 | `app/router.py`、`app/schemas.py` |
| BUG-004 | sweeper 读客户端自述 risk.level 决定超时放行 | 改读服务端信任等级（compute_trust），两个等级都写进留痕；EscalationPolicy 空校验补实 | `app/policies.py`、`app/sweeper.py`、`app/schemas.py` |
| BUG-005 | waiting_approval→running 无决策校验 | 从等待审批出发的任何流转必须带决策依据（approval_id 或 decided_by），否则 409 | `app/state.py`、`app/router.py` |
| BUG-006 | task-center 详情/转换/时间线/事件关联无租户过滤 | 统一 _get_or_404(..., tenant)；事件关联要求唯一命中，跨租户重名拒绝；建单幂等查询也限定租户 | `app/router.py` |
| BUG-007 | /wake 无状态守卫，会把闸门中断当 data_ready 消费 | 与 /resume 同款守卫：等待人审→409；非数据等待→409 | `app/main.py` |
| BUG-008 | Heuristic 恒定输出字母序方向，反向占优时方向错且带负值 | 取占优方向并取正依赖度 | `app/algorithms.py` |
| BUG-009 | α 候选集上限 2；冗余剔除要求两侧严格子集；短循环缺建模；回放非标准 | 上限可配(默认4)；冗余剔除改为「存在更大对」；补长度 1/2 循环库所(loop=true)；回放改 trace-driven 并区分循环携带 token | `app/algorithms.py` |
| BUG-010 | DFG 一致性 support_others < min_support-1 差一；空迹计为合规 | 判据改 support_others < min_support；默认 min_support=1；无边案例单列 not_evaluable 不计入分母 | `app/algorithms.py` |
| BUG-011 | 只捕获 httpx.HTTPError；规则化回退只认 3 种瓶颈类型 | 捕获全部异常并回退；rule_based 覆盖更多类型、gate_wait 与未知类型也产出提案、取值全部用 .get() | `app/proposals.py` |
| BUG-012 | /resume 从不写运行归档（根因），且 _archive_progress 未处理 rejected | resume 后归档；rejected 记 rejected；revise 记 waiting + revised 标记；success_rate 分母纳入 rejected | `app/main.py`、`app/runs.py` |
| BUG-013 | options 存而不用 | 决策前校验 decision 属于 options，否则 422 | `app/router.py` |
| BUG-015 | 空 external_ref 存空串撞唯一索引 → 500 | 空引用存 NULL；唯一性改 (tenant, external_ref) 复合唯一（轻量索引迁移）；TaskOut 字段可空 | `app/models.py`、`app/router.py`、`app/schemas.py`、`app/main.py` |

## 4. 修复过程中新发现并修复（原战役未覆盖）

| # | 问题 | 发现方式 | 处理 |
|---|---|---|---|
| N-1 | 建单幂等查询不带租户：B 产品线用同一外部引用会拿回 A 产品线的任务（跨租户读取） | 新增回归测试 | 已修：幂等查询限定租户 |
| N-2 | external_ref 为全局唯一，两个租户不能有相同单号（多租户语义错误，也正因此「歧义」从未发生） | 新增回归测试 | 已修：复合唯一 + 索引迁移 |
| N-3 | TaskOut.external_ref: str 与 NULL 冲突 → ResponseValidationError（同样是 500） | 复验探针 | 已修：类型改可空 |
| N-4 | /events 的任务关联可能跨租户改状态 | 原报告 BUG-006 的一部分 | 已修：X-Tenant 限定；无头时要求唯一命中，否则返回 ambiguous_ref |
| N-5 | 旧用例在断言「客户端可覆盖 mode」——正是被修掉的行为 | 跑既有测试 | 按新语义改写，并补「服务端灰度仍有效」断言 |

## 5. 探针修正（唯一非产品改动，显式标注）

- `probes.sh evidence` 子命令用 grep 找 `"ok": true`，而 `/evidence/verify` 的响应契约是 `{task_ref,total,verified,broken}` —— 该子命令**在任何版本上都会失败**；原战役的 EVD-001 卡用的是另一条按 JSON 字段判断的内联命令。
- 处理：先用卡内原命令重跑确认 PASS，再把这句子命令改为与契约一致的 JSON 判断（未改动任何原始证据文件）。

## 6. 回归测试

| 服务 | 测试数 | 新增防复发用例 |
|---|---|---|
| approval-gateway | 137 | +11（IM 签名正/负例、时间窗防重放、resubmit 租户、mode/persist 不可覆盖、options 强制、sweeper 新语义 2、反馈可信度 3） |
| task-center | 23 | +10（FSM 决策校验 4、租户隔离 2、external_ref 4） |
| agent-runtime | 175 | +4（/wake 守卫 2、归档 rejected/revise 2） |
| process-mining | 63 | +16（heuristic 3、alpha 5、DFG 3、LLM 回退 5） |
| 其它 3 个服务 | 56 | — |
| **全平台** | **454** | **+41** |

## 7. 结论与仍未验证的部分

**结论**：原报告 15 张 FAIL 卡对应的 15 个缺陷全部修复，逐卡用原探针复验通过；新增 41 条回归用例防复发；修复过程中另外发现并修复 5 个同源问题。

**但门禁不能仅凭本次复验判为通过**：

- 只重跑了失败卡与原通过卡；原报告的**性能场景未重跑**（性能数据仍是修复前基线）；
- 原报告列出的未覆盖维度依然未覆盖：真实 LLM 质量与提示注入、Qdrant 检索、GitLab MR 全生命周期、飞书真实推送、**并发/竞态**（decide / im_card_callback / sweep 均为无锁读改写）、Postgres 部署形态、公网 Caddy/SSO 入口、前端控制台 XSS、soak 稳定性；
- 本次修复引入一个新的**上线前置条件**：IM 回调现在必须配置签名密钥（`IM_CALLBACK_SIGNING_KEY` 或 `FEISHU_VERIFY_TOKEN`），未配置时会拒绝回调（503）——生产部署不要漏配。

**建议**：重跑完整战役（24 卡 + 性能 + 覆盖率）重新判定 G1/G2/G3，并补做并发与鉴权专项。

---

# 专项复验（2026-09-22，第 2 轮）：并发/竞态 + 鉴权

> 触发：第 7 节结论里"仍建议补做并发与鉴权专项"。本轮把这两个维度从"未覆盖"变成"有探针、有证据、有回归"。
> 原报告与第 1 轮复验报告均**不改动**，本轮结论并列记录在此。
> SUT：`bash test-campaign/start-sut.sh --backend postgres`（Postgres 后端 + `SERVICE_AUTH_MODE=token`）。

## 8. 并发/竞态专项

### 8.1 发现并修复的真实缺陷

| # | 缺陷 | 实测证据（修复前） | 根因 | 修法 |
|---|---|---|---|---|
| CONC-001 | 同一审批单被并发决策 → 落多条决策、多个 200 | 30 并发 × 8 轮：**6 轮**插入 2–5 条 `approval_decisions`、2–5 个请求返回 200 | 决策路径是"读状态→判 pending→写决策"，PG READ COMMITTED 下无互斥；`approval_decisions` 也没有唯一键 | ① 决策/重提/超时四条写路径统一 `SELECT … FOR UPDATE`；② 新增 `version` 列 + 唯一键 `uq_decision_request_version`（按提交版本唯一，兼容 revise 后 version+1）；③ 唯一键冲突转 409 |
| CONC-002 | 同一任务被并发流转 → 多次成功、时间线重复 | 30 并发 × 6 轮：**3 轮**有 2–15 个 200、时间线多出 1–14 条 `status_changed` | 与 CONC-001 同源：`transition` 无行锁 | `_get_or_404(..., for_update=True)` 用于流转路径；状态已变后重复流转仍 409 |

修复前 5 个 2–8 并发的小探针**全部 PASS** —— 这说明"没撞上"不等于没有竞态；因此本轮把并发量做成可配置的压测（`race_stress.py`），并把"必须恰好成功一次 + 数据库行数对账"作为判据。

### 8.2 复验结果

| 探针 | 并发量 | 修复前 | 修复后 |
|---|---|---|---|
| `decide-race-stress`（决策） | 30 × 8 轮 | 6/8 轮重复写入 | **0/8 轮重复（每轮恰好 1 个 200、1 条决策）** |
| `task-race-stress`（流转） | 30 × 6 轮 | 3/6 轮重复写入 | **0/6 轮重复** |
| `decide-race`（CONC-001，2–8 并发） | 8 | PASS（侥幸） | PASS |
| `decide-vs-sweep`（人工决策 vs 超时扫描） | 2 | PASS（侥幸） | PASS |
| `im-vs-decide`（签名 IM 回调 vs Web 决策） | 2 | PASS（侥幸） | PASS |
| `evidence-race`（并发上传证据） | 6 | PASS | PASS（6/6 落库） |
| `task-transition-race` | 4 | PASS（侥幸） | PASS |

### 8.3 新增回归用例（结构性断言 + 真实压测分工）

SQLite 单写者，多线程测不出真实语义；因此单测断言**结构保证**（行锁确实发出 `FOR UPDATE`、四条写路径都带锁、唯一键在元数据里、唯一键兜底可复现），真实并发交给 `race_stress.py` 打 PG 后端：

- `approval-gateway/tests/test_concurrency.py`（8 条）：含"预置同版本决策行 → 第二次决策 409 且库里只有 1 条"这类**确定性**兜底验证；
- `task-center/tests/test_concurrency.py`（4 条）：流转路径带锁 + 状态变更后重复流转仍 409 且时间线恰好 3 条。

## 9. 鉴权专项

### 9.1 清点结果（工具化，可进 CI）

新增 `platform/scripts/audit_endpoint_auth.py`（纯 AST，不启动服务）：

```
修复前：写接口 33 个，其中无鉴权 22 个（7 个服务里 6 个完全裸奔）
修复后：写接口 33 个，其中无鉴权 0 个
        （22 个由统一中间件接管；逐路由依赖口径仍是 22，正是中间件存在的意义）
```

无鉴权清单里包括：`POST /console/decide`（替人落决策）、`POST /tools/invoke`（调工具）、
`POST /mining/proposals/apply`（改 SOP 配置）、`POST /runs/*/resume`（恢复人审挂起）、
`POST /alerts/prometheus`（凭空拉运维 SOP）、`POST /gitlab/*`（用平台的 GitLab Token 改仓库）。

### 9.2 发现并修复的真实缺陷

| # | 缺陷 | 实测证据 | 修法 |
|---|---|---|---|
| AUTHZ-001 | 6 个服务的全部写接口无鉴权 | 清点 22/33 无鉴权；`X-Service-Token` 这个头**没有任何服务在读**（探针脚本里带着它，纯属自我安慰） | 新增统一中间件 `app/service_auth.py`（7 服务各一份，`scripts/sync_service_auth.py --check` 防漂移）：`SERVICE_AUTH_MODE=token` 时**默认拒绝、显式豁免**，新增路由自动受保护；`off` 模式启动即打 WARNING 明示"当前无服务间鉴权" |
| AUTHZ-002 | 开了令牌模式后服务间调用全 401（把功能也关掉） | `POST /runs` 返回 500，日志显示 agent-runtime→网关 `/approvals` 401 | 中间件模块提供 `outbound_headers()`，所有出站客户端（审批/A 闸门/GitLab/知识库/事件发布/调度器）统一带上服务令牌 |
| AUTHZ-003 | 人审闸门可被"自述决策"绕过 | `/resume` 只要挂在 Gate 上就接受任意调用方的 `decision=approve`（`/wake` 早已加守卫，`/resume` 没有） | 新增 `app/gate_decisions.py`：`GATE_DECISION_VERIFY=off\|warn\|block`（token 模式默认 block），核验审批单**存在 + 状态与决策一致 + task_ref 一致 + Gate 类型与挂起节点一致**；核验依据写进运行归档 |
| AUTHZ-004 | 工具调用的岗位与"免人审"由请求体自述 | `{role:"ops-duty", allow_ungated:true}` 可自称任意岗位、自称免人审 | `allow_ungated` 默认不采信（需 `TOOLS_ALLOW_UNGATED=1`）；受管控部署下 `role` 也不采信（需 `TOOLS_ALLOW_CLIENT_ROLE=1`），并留审计 |
| AUTHZ-005 | 告警 webhook 完全裸奔 | `demo_alert.py` 里的 `WEBHOOK_SECRET` 只给了 gitlab-bridge，task-center 从未读取 | 新增 `app/alert_auth.py`：`X-Webhook-Token` 或 `X-Webhook-Signature: sha256=<hmac>`，未配置密钥则 fail-closed(503)；更新 demo 与用例 |
| AUTHZ-006 | OIDC 只校验"登录了没有"，不校验"是谁" | 任何登录用户（含只读账号）可批准审批、可触发**跨全部租户**的超时扫描 | 新增 `require_role()`（`APPROVER_ROLES` 默认 approver,admin）用于 `/approvals/{id}/decision` 与 `/admin/sweep`；`AUTH_MODE=off/static` 行为不变 |
| AUTHZ-007 | 审计署名可冒名 | OIDC 下 `decided_by` 取自请求体，登录用户可署他人姓名 | 身份真实时署名以 token 的 `sub` 为准，不一致时告警 |
| AUTHZ-008 | 驾驶舱转调网关不带身份 | `AUTH_MODE=oidc` 时驾驶舱"批准"按钮必然 502（网关 401）；放行模式下也无从记录是谁批的 | 透传 `Authorization`/`X-Tenant`/`X-Service-Token`/`X-Platform-User`/`Cookie` |
| AUTHZ-009 | 驾驶舱 HTML 存储型 XSS | 上游可被用户左右的标题/需求文本/阶段名直接拼进 HTML | 新增 `esc()`，所有插值统一转义（含 `onclick` 参数） |
| AUTHZ-010 | 默认配置指向错误端口 | `APPROVAL_GATEWAY_URL` 默认 `localhost:8000`，而本机一键起栈的网关是 **8001**（`run_local_stack.py` 端口表） | agent-runtime、process-mining 默认值对齐 8001；`GITLAB_BRIDGE_URL` 对齐 8006 |
| AUTHZ-011 | 公网入口可被伪造可信头 | `Caddyfile` 未剥离 `X-Platform-User`/`X-Service-Token` | 入口统一 `header_up -X-...` 剥离，SSO 注入点显式标注；`.env.prod.example` 补全 8 个新变量与说明 |

### 9.3 复验结果

| 探针（`probes-authz.sh`） | 修复前 | 修复后 |
|---|---|---|
| `open-writes` | 22/33 写接口无鉴权 | **0/33**（22 个由中间件接管，豁免清单可审） |
| `no-token`（6 个代表端点 + /health） | 全部 200（裸奔） | **无令牌 401、/health 200** |
| `with-token` | — | 带令牌的建单→决策链路 **200**（鉴权没把功能关掉） |
| `resume-bypass` | 自述 `approve` 直接恢复执行 | 无 `approval_id` / 伪造编号均 **409**，且闸门仍在原地 |
| `tools-selfdeclared` | 自述岗位、自述免人审均被采信 | **403**（`ungated_not_allowed` / `role_not_trusted`） |
| `alert-signature` | 无凭据也能拉运维 SOP | 无凭据/错密钥 **401**；正确令牌与 HMAC **200** |
| `decide-race`（交叉验证） | — | PASS |

### 9.4 对既有 18 个探针的影响（全部复验）

打开令牌模式后，原 `probes.sh` 的请求不带服务令牌，会全部 401。处理方式与结果：

- `probes.sh` 统一注入 `X-Service-Token`（用 shell 函数包装 `curl`，避免漏改一处就得出"假 401"结论）；
- **加固后的 SUT 上 18/18 全部 PASS**（`verify-after/hardened-*.txt`）；
- `reject-archive` 探针**按更严的新语义改写**：先在审批台上真做一次 `reject`，再拿 `approval_id` 恢复运行 —— 这比原来"直接自述 reject"更接近真实链路，属于**探针变强**而非放宽（其余 17 个探针的断言一字未改）。

## 10. 本轮回归总量

| 服务 | 测试数 | 本轮新增 |
|---|---|---|
| approval-gateway | 151 | +14（并发 8、鉴权 6） |
| task-center | 38 | +15（并发 4、鉴权 11） |
| agent-runtime | 190 | +15（鉴权 14、工具语义 1 改写） |
| process-mining | 68 | +5（鉴权） |
| metrics-dashboard | 22 | +9（身份透传、XSS、令牌） |
| knowledge-base | 23 | +6（令牌） |
| gitlab-bridge | 28 | +4（令牌 + webhook 豁免自查） |
| **全平台** | **520** | **+66** |

另：9 个演练（drills）复跑，`drill_eng_gate_e1` 因"客户端 mode 覆盖已被禁止"这一更严语义**同步更新为**：⑤ 客户端覆盖被忽略（服务端权威）+ ⑤b 服务端 `ENG_GATES_MODE=warn` 才灰度 → 11/11 通过；其余 8 个全绿。

## 11. 本轮之后仍未验证的部分（诚实清单）

- **公网部署形态**：Caddy 已补剥离头与 SSO 注入点，但**没有真跑过**公网入口 + oauth2-proxy 组合；`TRUST_PROXY_AUTH=1` 的正确性依赖入口剥离头，属于"配置正确才安全"，需要一次真实部署验证；
- **真实 LLM 质量 / 提示注入、Qdrant 检索质量、GitLab MR 全生命周期、飞书真实推送、soak 稳定性**：仍未覆盖；
- **多实例水平扩展**：行锁与唯一键在单库上成立；多副本 + 读写分离时的语义未验证；
- **性能**：本轮压测只针对**正确性**（是否重复写入），没有测吞吐/尾延迟；原性能基线仍是修复前数据；
- **前端**：驾驶舱 XSS 已修，但审批台单页（`approval-gateway/app/static/index.html`）未做同类审计。

**结论**：并发/竞态与鉴权两个维度从"未覆盖"变为"有探针、有修复、有回归、有前后对照证据"。但门禁判定仍需重跑完整战役（24 卡 + 性能 + 覆盖率），本轮不改变 G1/G2/G3 的既有结论。

**新增上线前置条件（务必配置，否则相应功能会 fail-closed）**：
`SERVICE_AUTH_MODE=token` + `SERVICE_AUTH_TOKEN`、`ALERT_WEBHOOK_SECRET`、`IM_CALLBACK_SIGNING_KEY`（或 `FEISHU_VERIFY_TOKEN`）、
`GATE_DECISION_VERIFY=block`、`TRUST_PROXY_AUTH=1`（且入口剥离同名头）；`TOOLS_ALLOW_UNGATED`/`TOOLS_ALLOW_CLIENT_ROLE` 保持 0。
