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
