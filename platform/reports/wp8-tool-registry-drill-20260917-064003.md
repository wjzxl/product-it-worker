# WP8 工具注册表与执行期鉴权演练报告

- 执行时间：2026-09-17T06:40:03.320277+00:00
- 工具总数：11｜写类：['approval.create', 'doc.write', 'gitlab.branch_mr', 'ops.execute', 'sandbox.pytest']｜需人审：['ops.execute']
- 对应课程：图 59「两种边界」（看不见 / 不能改）、图 62「工具说明」

## 验收结果（8/8）

| 验收项 | 结果 | 证据 |
|---|---|---|
| ① 工具注册表加载 | ✅ | 11 个工具，需人审：['ops.execute'] |
| ② 每个工具都登记了权限点/参数类型/失败模式 | ✅ | 写类工具：['approval.create', 'doc.write', 'gitlab.branch_mr', 'ops.execute', 'sandbox.pytest'] |
| ③ 运维助理看不到生产变更工具（看不见） | ✅ | 可见：['approval.create', 'kb.search', 'loki.query', 'prometheus.query'] |
| ④ 四类越权/非法调用全部被拒 | ✅ | ops.execute→role_not_allowed；gitlab.branch_mr→role_not_allowed；prometheus.query→missing_params；不存在.tool→unregistered |
| ⑤ 拒绝都留了审计（可查证） | ✅ | 4 条拒绝记录，最近一条：{"tool": "不存在.tool", "role": "ops-duty", "outcome": "denied", "reason": "unregistered", "params": {}} |
| ⑥ 危险动作无审批被拒、带 G5 审批号放行 | ✅ | 无审批→approval_required；带审批→{'executed': True, 'action': 'restart', 'target': 'order-api', 'approval_ref': 'apv_g5_001'} |
| ⑦ 新增工具只需加 YAML（不改代码） | ✅ | 新注册表：['kb.search', 'report.generate'] |
| ⑧ 岗位三件套与工具注册表双向一致 | ✅ | dev-agent:4；ops-duty:4；product-agent:4 |

## 审计日志（含被拒记录）

| 工具 | 岗位 | 结果 | 原因 |
|---|---|---|---|
| ops.execute | ops-duty | denied | role_not_allowed |
| gitlab.branch_mr | ops-duty | denied | role_not_allowed |
| prometheus.query | ops-duty | denied | missing_params |
| 不存在.tool | ops-duty | denied | unregistered |
| ops.execute | ops-lead | denied | approval_required |
| ops.execute | ops-lead | ok |  |

## 结论

全部通过：边界在执行期强制，且新增工具无需改代码。
