# Coverage and traceability matrix

| Requirement | Pri | Card | Layer | Verdict | Evidence |
|---|---|---|---|---|---|
| REQ-001 — 服务健康且如实报告自身状态（含治理姿态与 checkpointer） | P1 | REG-018 | api | `PASS` | evidence/70518e6a423d47f4a8a3a749f1811326/REG-018.cmd.txt |
| REQ-001 — 服务健康且如实报告自身状态（含治理姿态与 checkpointer） | P1 | OPS-503 | other | `PASS` | evidence/2022f58f8cf540e2876c70fcdc1e6735/OPS-503.cmd.txt |
| REQ-002 — 未鉴权请求不得触达业务读写接口 | P0 | SEC-101 | security | `PASS` | evidence/023feaccafa94a12bfc30ab950f83fd9/SEC-101.cmd.txt |
| REQ-002 — 未鉴权请求不得触达业务读写接口 | P0 | SEC-102 | security | `PASS` | evidence/50aa7c7a53af41c4b8a2e8c661f8902a/SEC-102.cmd.txt |
| REQ-002 — 未鉴权请求不得触达业务读写接口 | P1 | DEP-604 | other | `PASS` | evidence/5a8b5aa604bd4c67a4a11fa3122b614f/DEP-604.cmd.txt |
| REQ-003 — 鉴权不得把服务间调用一起关掉 | P0 | SEC-103 | security | `PASS` | evidence/528c86e2d7824a5fa92c8f193a5d008e/SEC-103.cmd.txt |
| REQ-003 — 鉴权不得把服务间调用一起关掉 | P1 | FLOW-804 | flow | `PASS` | evidence/01f417db35004065895731be35c3d978/FLOW-804.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | REG-001 | api | `PASS` | evidence/f54b1da87ba54712ac4aa67c81ba048c/REG-001.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | REG-006 | api | `PASS` | evidence/a47d750403334f1287865cdced969c57/REG-006.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | REG-012 | api | `PASS` | evidence/8d5fe53a67d048108d7359d1efda499c/REG-012.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | SEC-104 | security | `PASS` | evidence/c34115007688405989d254c1b63fdd54/SEC-104.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | UI-309 | e2e | `PASS` | evidence/04671aaf14774dbbafd8a6964221a98d/UI-309.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | INT-805 | integration | `PASS` | evidence/6374ee3564494f31a34f50bdd9fda6bd/INT-805.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | FLOW-801 | flow | `PASS` | evidence/221398c5e7bd453faa70fbc41929ca15/FLOW-801.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P1 | FLOW-807 | flow | `PASS` | evidence/f21bf446547945c9992dbb64aa538777/FLOW-807.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P1 | FLOW-802 | flow | `FAIL` | evidence/539c5fde025a42adabdd20c1eec2b763/FLOW-802.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | FLOW-803 | flow | `PASS` | evidence/3fc7669fbc424454bbaa85fa1146f3eb/FLOW-803.cmd.txt |
| REQ-004 — 人审闸门不可被自述决策绕过 | P0 | FLOW-806 | flow | `PASS` | evidence/579c57f94b254b8aa3d42de44d0420b5/FLOW-806.cmd.txt |
| REQ-005 — 工程闸门宽严由服务端决定，且每次判定留痕 | P0 | REG-004 | api | `PASS` | evidence/6dfdf367204d42dd8652a05dd68d12e4/REG-004.cmd.txt |
| REQ-005 — 工程闸门宽严由服务端决定，且每次判定留痕 | P0 | REG-005 | api | `PASS` | evidence/8c9dbbb9b95243b1be8457a94f3f3374/REG-005.cmd.txt |
| REQ-006 — 任务状态机拒绝非法流转，等待审批不得被跳过 | P0 | REG-007 | api | `PASS` | evidence/09170f56db514bfb97df2a66f596e899/REG-007.cmd.txt |
| REQ-006 — 任务状态机拒绝非法流转，等待审批不得被跳过 | P1 | REG-008 | api | `PASS` | evidence/8a8c35460c8f44a08b8a4d3a6a4029e4/REG-008.cmd.txt |
| REQ-006 — 任务状态机拒绝非法流转，等待审批不得被跳过 | P0 | INT-802 | integration | `PASS` | evidence/c9474c37c9594e1e94f01e6fab494ed5/INT-802.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P0 | REG-002 | api | `PASS` | evidence/881e6251bbcc428681e457fa5c7756d1/REG-002.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P1 | REG-003 | api | `PASS` | evidence/60a3f71f49b64bd2849a0ad13f6141d7/REG-003.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P0 | REG-009 | api | `PASS` | evidence/90b6e8e17a03447392d35299107470db/REG-009.cmd.txt |
| REQ-007 — 跨租户读写必须被隔离 | P1 | REG-010 | api | `PASS` | evidence/2c8f9471009f407b85b74db3643ac6bc/REG-010.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-201 | stability | `PASS` | evidence/f9a587c99fda4299a30ced7373df9381/CONC-201.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-202 | stability | `PASS` | evidence/1ae252f962974cd5aa6efc57b189cc04/CONC-202.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-203 | stability | `PASS` | evidence/166d5ff6e751406a8ad81d512cf0c68f/CONC-203.cmd.txt |
| REQ-008 — 并发下同一单据只产生一个决策（不重复写入） | P0 | CONC-204 | stability | `PASS` | evidence/ff4562ba31b0409f9c97b58283cb32e3/CONC-204.cmd.txt |
| REQ-009 — 工具调用权限由服务端判定，不接受请求体自述 | P0 | SEC-105 | security | `PASS` | evidence/a3eb99703b2143fcbbcfdc69bf827540/SEC-105.cmd.txt |
| REQ-010 — 运行结果与成本如实归档（否决可见） | P0 | REG-011 | api | `PASS` | evidence/12bd7139ce7d49c0b7035356e401f9de/REG-011.cmd.txt |
| REQ-010 — 运行结果与成本如实归档（否决可见） | P1 | DATA-405 | data | `PASS` | evidence/9ebd8a42ab254d6da6c9176c24c8e419/DATA-405.cmd.txt |
| REQ-010 — 运行结果与成本如实归档（否决可见） | P0 | FLOW-801 | flow | `PASS` | evidence/221398c5e7bd453faa70fbc41929ca15/FLOW-801.cmd.txt |
| REQ-011 — 证据可复验（哈希一致） | P1 | REG-017 | api | `PASS` | evidence/4904df297a674879b552ff1b9121d71a/REG-017.cmd.txt |
| REQ-011 — 证据可复验（哈希一致） | P1 | CONC-205 | stability | `PASS` | evidence/9f6eb99cff09495089478bbb11468fb1/CONC-205.cmd.txt |
| REQ-011 — 证据可复验（哈希一致） | P1 | DATA-404 | data | `PASS` | evidence/abbecfb2ea9c4eb1b5319ef5e21b4a7e/DATA-404.cmd.txt |
| REQ-012 — 来源链可反查 | P1 | PROV-901 | api | `PASS` | evidence/3190d3c6b8e54441a81fc5e69b65653d/PROV-901.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-013 | api | `PASS` | evidence/e379122af9b34b2caf83c5b67383f7e6/REG-013.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-014 | api | `PASS` | evidence/0a63f332c740412bb247182de18b498c/REG-014.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-015 | api | `PASS` | evidence/cb9a6bf7a2c84af28f75ea36e4dc3fdd/REG-015.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | REG-016 | api | `PASS` | evidence/8bbb877786d14f33bfb9c11ebbad4f56/REG-016.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | INT-804 | integration | `PASS` | evidence/1f9a46defd004e5683164cf49f24c642/INT-804.cmd.txt |
| REQ-013 — 流程挖掘结论正确且提案不会绕过 G7 | P1 | FLOW-804 | flow | `PASS` | evidence/01f417db35004065895731be35c3d978/FLOW-804.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | SEC-107 | security | `PASS` | evidence/3de243a1c522445787fba017fd3490c5/SEC-107.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-301 | e2e | `PASS` | evidence/81629d65faa34765ab6bae44e3f5ac56/UI-301.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-302 | e2e | `PASS` | evidence/e3b826f8a34b4610915d39c25c21ccca/UI-302.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-303 | e2e | `PASS` | evidence/4c2062697ff240f7875aceae1bf17f40/UI-303.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-304 | e2e | `PASS` | evidence/f24ac4b4a419460493f9c9d95da051a5/UI-304.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P1 | UI-305 | e2e | `PASS` | evidence/d6a1f383e4124cf98afb790b2fa401c0/UI-305.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P1 | UI-306 | e2e | `PASS` | evidence/157c99a9b89e4483a8471a4435c48939/UI-306.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-309 | e2e | `PASS` | evidence/04671aaf14774dbbafd8a6964221a98d/UI-309.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-308 | e2e | `PASS` | evidence/7c0ee7ffd4184864b6ba9d5287fdfcd9/UI-308.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | UI-307 | e2e | `PASS` | evidence/5dbf1f386d294acaa2f322fcaabb9531/UI-307.cmd.txt |
| REQ-014 — 审批台在有数据时必须显示待办，且不得渲染注入内容 | P0 | FLOW-805 | flow | `PASS` | evidence/25b1561c443b45ecaa5ea259106c62ef/FLOW-805.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P0 | UI-311 | e2e | `PASS` | evidence/1260503ba7014f689ebd47a32a71ee1c/UI-311.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P0 | UI-312 | e2e | `PASS` | evidence/bd23cff961e44509b745bd79347a5342/UI-312.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P2 | UI-313 | e2e | `PASS` | evidence/8ac86bc7e46f417c8aac0dda4abf6a68/UI-313.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P2 | UI-314 | e2e | `PASS` | evidence/9100a1fd7e9e4a0ca82c83873c79d308/UI-314.cmd.txt |
| REQ-015 — 驾驶舱在文档所述入口拓扑下可读 | P0 | INT-805 | integration | `PASS` | evidence/6374ee3564494f31a34f50bdd9fda6bd/INT-805.cmd.txt |
| REQ-016 — 告警入口必须验签，未配密钥不得放行 | P1 | SEC-106 | security | `PASS` | evidence/dfb81c2a4e8241fe9265c080b37754b3/SEC-106.cmd.txt |
| REQ-016 — 告警入口必须验签，未配密钥不得放行 | P0 | INT-801 | integration | `PASS` | evidence/dedeeb5a45b64b25b5b155805bd25e17/INT-801.cmd.txt |
| REQ-016 — 告警入口必须验签，未配密钥不得放行 | P0 | FLOW-803 | flow | `PASS` | evidence/3fc7669fbc424454bbaa85fa1146f3eb/FLOW-803.cmd.txt |
| REQ-017 — GitLab webhook 必须验签 | P1 | SEC-109 | security | `PASS` | evidence/b8fb7fb2556b44f085363fbdd71c5180/SEC-109.cmd.txt |
| REQ-018 — 知识库入库/检索可用且来源可追溯 | P1 | INT-803 | integration | `PASS` | evidence/07dc87b62c674441b362c4d0fbae678d/INT-803.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P0 | DATA-401 | data | `PASS` | evidence/8abccfbadc28461886458718972a46f2/DATA-401.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P0 | DATA-402 | data | `PASS` | evidence/d7522aceca264228a65d7665fc9f3cf7/DATA-402.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P1 | DATA-405 | data | `PASS` | evidence/9ebd8a42ab254d6da6c9176c24c8e419/DATA-405.cmd.txt |
| REQ-019 — 空库可建、模型与迁移零漂移、可回滚、备份可恢复 | P0 | DATA-403 | data | `PASS` | evidence/f1117d6fa5104d9bbe6bd0c63c978931/DATA-403.cmd.txt |
| REQ-020 — 平台自身故障可被发现并通知 | P1 | OPS-501 | other | `PASS` | evidence/d5422168970e4fc4a2aaafdee7e32716/OPS-501.cmd.txt |
| REQ-020 — 平台自身故障可被发现并通知 | P1 | OPS-502 | other | `PASS` | evidence/49de238aa8ac4752bfbc56e49cc38a5c/OPS-502.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P1 | OPS-503 | other | `PASS` | evidence/2022f58f8cf540e2876c70fcdc1e6735/OPS-503.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P0 | OPS-504 | other | `PASS` | evidence/10acd258f1824bc891b9a1a58974045c/OPS-504.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P0 | DEP-601 | other | `PASS` | evidence/17b726c2e7dd48d59fb4258cc9af608f/DEP-601.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P1 | DEP-602 | other | `PASS` | evidence/b81f06dca6c44872b46fc727c52ab00f/DEP-602.cmd.txt |
| REQ-021 — 生产档位下不得带着宽松开关上线 | P0 | DEP-603 | other | `PASS` | evidence/a9ccdda7c9bf42a4a6a56c76bee4b9f5/DEP-603.cmd.txt |
| REQ-022 — 外部身份头不得被直连客户端伪造 | P0 | SEC-108 | security | `PASS` | evidence/b150a1e45b374176a3a9d311ee3969a7/SEC-108.cmd.txt |

## Cards not linked to any requirement

_Every card traces to a requirement._
