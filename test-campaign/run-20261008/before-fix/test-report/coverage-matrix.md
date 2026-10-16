# Coverage and traceability matrix

| Requirement | Pri | Card | Layer | Verdict | Evidence |
|---|---|---|---|---|---|
| REQ-001 — 服务健康且如实报告自身状态（含治理姿态与 checkpointer） | P1 | REG-018 | api | `PASS` | cards/REG-018.cmd.txt |
| REQ-001 — 服务健康且如实报告自身状态（含治理姿态与 checkpointer） | P1 | OPS-503 | ops | `PASS` | cards/OPS-503.cmd.txt |
| REQ-002 — 未鉴权请求不得触达业务读写接口 | P0 | SEC-101 | security | `PASS` | cards/SEC-101.cmd.txt |
| REQ-002 — 未鉴权请求不得触达业务读写接口 | P0 | SEC-102 | security | `PASS` | cards/SEC-102.cmd.txt |
| REQ-002 — 未鉴权请求不得触达业务读写接口 | P1 | DEP-604 | deploy | `PASS` | cards/DEP-604.cmd.txt; cards/DEP-604.cmd.txt |
| REQ-003 — 鉴权不得把服务间调用一起关掉 | P0 | SEC-103 | security | `PASS` | cards/SEC-103.cmd.txt |
| REQ-003 — 鉴权不得把服务间调用一起关掉 | P1 | FLOW-804 | e2e | `FAIL` | cards/FLOW-804.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | REG-001 | api | `PASS` | cards/REG-001.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | REG-006 | api | `PASS` | cards/REG-006.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | REG-012 | api | `PASS` | cards/REG-012.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | SEC-104 | security | `PASS` | cards/SEC-104.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | INT-805 | integration | `PASS` | cards/INT-805.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | FLOW-801 | e2e | `PASS` | cards/FLOW-801.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P1 | FLOW-807 | e2e | `PASS` | cards/FLOW-807.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P1 | FLOW-802 | e2e | `FAIL` | cards/FLOW-802.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | FLOW-803 | e2e | `PASS` | cards/FLOW-803.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | FLOW-806 | e2e | `FAIL` | cards/FLOW-806.cmd.txt |
| REQ-005 — 工程闸门宽严由服务端决定，且每次判定留痕 | P0 | REG-004 | api | `PASS` | cards/REG-004.cmd.txt |
| REQ-005 — 工程闸门宽严由服务端决定，且每次判定留痕 | P0 | REG-005 | api | `PASS` | cards/REG-005.cmd.txt |
| REQ-006 — 任务状态机拒绝非法流转，等待审批不得被跳过 | P0 | REG-007 | api | `PASS` | cards/REG-007.cmd.txt |
| REQ-006 — 任务状态机拒绝非法流转，等待审批不得被跳过 | P1 | REG-008 | api | `PASS` | cards/REG-008.cmd.txt |
| REQ-006 — 任务状态机拒绝非法流转，等待审批不得被跳过 | P0 | INT-802 | integration | `PASS` | cards/INT-802.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P0 | REG-002 | api | `PASS` | cards/REG-002.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P1 | REG-003 | api | `PASS` | cards/REG-003.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P0 | REG-009 | api | `PASS` | cards/REG-009.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P1 | REG-010 | api | `PASS` | cards/REG-010.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-201 | concurrency | `PASS` | cards/CONC-201.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-202 | concurrency | `PASS` | cards/CONC-202.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-203 | concurrency | `PASS` | cards/CONC-203.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-204 | concurrency | `PASS` | cards/CONC-204.cmd.txt |
| REQ-009 — 工具调用权限由服务端判定，不接受请求体自述 | P0 | SEC-105 | security | `PASS` | cards/SEC-105.cmd.txt |
| REQ-010 — 运行结果与成本如实归档（否决可见） | P0 | REG-011 | api | `PASS` | cards/REG-011.cmd.txt |
| REQ-010 — 运行结果与成本如实归档（否决可见） | P0 | FLOW-801 | e2e | `PASS` | cards/FLOW-801.cmd.txt |
| REQ-010 — 运行结果与成本如实归档（否决可见） | P1 | DATA-405 | data | `FAIL` | cards/DATA-405.cmd.txt |
| REQ-011 — 证据可复验（哈希一致） | P1 | REG-017 | api | `PASS` | cards/REG-017.cmd.txt |
| REQ-011 — 证据可复验（哈希一致） | P1 | CONC-205 | concurrency | `PASS` | cards/CONC-205.cmd.txt |
| REQ-011 — 证据可复验（哈希一致） | P1 | DATA-404 | data | `PASS` | cards/DATA-404.cmd.txt |
| REQ-012 — 来源链可反查 | P1 | PROV-901 | api | `PASS` | cards/PROV-901.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-013 | api | `PASS` | cards/REG-013.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-014 | api | `PASS` | cards/REG-014.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-015 | api | `PASS` | cards/REG-015.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-016 | api | `PASS` | cards/REG-016.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | INT-804 | integration | `PASS` | cards/INT-804.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | FLOW-804 | e2e | `FAIL` | cards/FLOW-804.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | SEC-107 | security | `PASS` | cards/SEC-107.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-301 | e2e | `FAIL` | cards/UI-301.cmd.txt; cards/UI-301.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-302 | e2e | `FAIL` | cards/UI-302.cmd.txt; cards/UI-302.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-303 | e2e | `FAIL` | cards/UI-303.cmd.txt; cards/UI-303.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-304 | e2e | `FAIL` | cards/UI-304.cmd.txt; cards/UI-304.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P1 | UI-305 | e2e | `FAIL` | cards/UI-305.cmd.txt; cards/UI-305.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P1 | UI-306 | e2e | `PASS` | cards/UI-306.cmd.txt; cards/UI-306.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | FLOW-805 | e2e | `FAIL` | cards/FLOW-805.cmd.txt; cards/FLOW-805.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-307 | e2e | `PASS` | cards/UI-307.cmd.txt; cards/UI-307.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P0 | UI-311 | e2e | `PASS` | cards/UI-311.cmd.txt; cards/UI-311.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P0 | UI-312 | e2e | `PASS` | cards/UI-312.cmd.txt; cards/UI-312.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P2 | UI-313 | e2e | `PASS` | cards/UI-313.cmd.txt; cards/UI-313.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P2 | UI-314 | e2e | `PASS` | cards/UI-314.cmd.txt; cards/UI-314.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P0 | INT-805 | integration | `PASS` | cards/INT-805.cmd.txt |
| REQ-016 — 告警入口必须验签，未配密钥不得放行 | P1 | SEC-106 | security | `PASS` | cards/SEC-106.cmd.txt |
| REQ-016 — 告警入口必须验签，未配密钥不得放行 | P0 | INT-801 | integration | `PASS` | cards/INT-801.cmd.txt |
| REQ-016 — 告警入口必须验签，未配密钥不得放行 | P0 | FLOW-803 | e2e | `PASS` | cards/FLOW-803.cmd.txt |
| REQ-017 — GitLab webhook 必须验签 | P1 | SEC-109 | security | `PASS` | cards/SEC-109.cmd.txt |
| REQ-018 — 知识库入库/检索可用且来源可追溯 | P1 | INT-803 | integration | `PASS` | cards/INT-803.cmd.txt; cards/INT-803.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P0 | DATA-401 | data | `PASS` | cards/DATA-401.cmd.txt; cards/DATA-401.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P0 | DATA-402 | data | `PASS` | cards/DATA-402.cmd.txt; cards/DATA-402.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P0 | DATA-403 | data | `PASS` | cards/DATA-403.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P1 | DATA-405 | data | `FAIL` | cards/DATA-405.cmd.txt |
| REQ-020 — 平台自身故障可被发现并通知 | P1 | OPS-501 | ops | `PASS` | cards/OPS-501.cmd.txt |
| REQ-020 — 平台自身故障可被发现并通知 | P1 | OPS-502 | ops | `PASS` | cards/OPS-502.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P1 | OPS-503 | ops | `PASS` | cards/OPS-503.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P0 | OPS-504 | ops | `PASS` | cards/OPS-504.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P0 | DEP-601 | deploy | `PASS` | cards/DEP-601.cmd.txt; cards/DEP-601.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P1 | DEP-602 | deploy | `PASS` | cards/DEP-602.cmd.txt; cards/DEP-602.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P0 | DEP-603 | deploy | `PASS` | cards/DEP-603.cmd.txt |
| REQ-022 — 外部身份头不得被直连客户端伪造 | P0 | SEC-108 | security | `FAIL` | cards/SEC-108.cmd.txt |

## Cards not linked to any requirement

_Every card traces to a requirement._
