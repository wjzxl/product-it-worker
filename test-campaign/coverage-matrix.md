# Coverage and traceability matrix

| Requirement | Pri | Card | Layer | Verdict | Evidence |
|---|---|---|---|---|---|
| REQ-001 — 审批闸门可正常创建与查询 | P0 | BASE-001 | api | `PASS` | evidence/BASE-001.http.txt |
| REQ-001 — 审批闸门可正常创建与查询 | P0 | BASE-002 | api | `PASS` | evidence/BASE-002.http.txt |
| REQ-002 — 人工批准能生效并留痕决策人 | P0 | BASE-003 | api | `PASS` | evidence/BASE-003.http.txt |
| REQ-003 — 拒绝/退回必须附原因 | P1 | BASE-004 | api | `PASS` | evidence/BASE-004.cmd.txt |
| REQ-004 — 闸门决策必须经鉴权，调用方不能自免门禁 | P0 | SEC-001 | security | `FAIL` | evidence/SEC-001.cmd.txt |
| REQ-004 — 闸门决策必须经鉴权，调用方不能自免门禁 | P0 | SEC-004 | security | `FAIL` | evidence/SEC-004.cmd.txt |
| REQ-005 — 租户之间完全隔离，不得跨租户读写 | P0 | SEC-002 | security | `PASS` | evidence/SEC-002.cmd.txt |
| REQ-005 — 租户之间完全隔离，不得跨租户读写 | P0 | SEC-003 | security | `FAIL` | evidence/SEC-003.cmd.txt |
| REQ-005 — 租户之间完全隔离，不得跨租户读写 | P0 | FSM-003 | security | `FAIL` | evidence/FSM-003.cmd.txt |
| REQ-006 — 每次闸门检查都留审计记录 | P0 | GATE-001 | security | `FAIL` | evidence/GATE-001.cmd.txt |
| REQ-007 — 超时策略不得由单据自述风险等级驱动自动放行 | P0 | GATE-002 | security | `FAIL` | evidence/GATE-002.cmd.txt |
| REQ-008 — 单据声明的可选决策被强制执行 | P1 | GATE-003 | api | `FAIL` | evidence/GATE-003.cmd.txt |
| REQ-009 — 等待审批的任务不能无决策恢复执行 | P0 | FSM-001 | api | `FAIL` | evidence/FSM-001.cmd.txt |
| REQ-010 — 非法状态转换被拒绝 | P1 | FSM-002 | api | `PASS` | evidence/FSM-002.cmd.txt |
| REQ-011 — 重复或空的 external_ref 被优雅处理 | P1 | FSM-004 | api | `FAIL` | evidence/FSM-004.cmd.txt |
| REQ-012 — 运行归档状态如实反映被否决的结果 | P0 | RT-001 | data | `FAIL` | evidence/RT-001.cmd.txt |
| REQ-013 — 数据唤醒不得改变等待审批运行的结果 | P0 | RT-002 | api | `FAIL` | evidence/RT-002.cmd.txt |
| REQ-014 — 健康检查如实报告持久化后端 | P1 | RT-003 | api | `PASS` | evidence/RT-003.cmd.txt |
| REQ-015 — Heuristic Miner 输出正确依赖方向且能接受自身日志 | P0 | ALG-001 | data | `FAIL` | evidence/ALG-001.cmd.txt |
| REQ-016 — α-Miner 能回放自身挖掘所用日志 | P0 | ALG-002 | data | `FAIL` | evidence/ALG-002.cmd.txt |
| REQ-017 — 默认一致性检查能识别唯一异常路径 | P1 | ALG-003 | data | `FAIL` | evidence/ALG-003.cmd.txt |
| REQ-018 — LLM 不可用或响应畸形时回退规则化 | P1 | ALG-004 | api | `FAIL` | evidence/ALG-004.cmd.txt |
| REQ-019 — 审批网关稳态负载满足延迟与错误率预算 | P1 | PT-001 | performance | `PASS` | evidence/PT-001.cmd.txt |
| REQ-020 — 证据存证可上传、可校验、篡改可发现 | P1 | EVD-001 | api | `PASS` | evidence/EVD-001.cmd.txt |

## Cards not linked to any requirement

_Every card traces to a requirement._
