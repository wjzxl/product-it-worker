# WP7 持久化运行时演练报告

- 执行时间：2026-09-17T06:40:12.794644+00:00
- checkpointer：`{"configured": "postgres", "dsn": "127.0.0.1:5432/platform", "restart_safe": true, "note": "状态落 PG，进程重启可恢复"}`
- 线程：`wp7-drill-1789627203`（产品域）｜PG DSN：`127.0.0.1:5432/platform`
- 对应课程：图 48（等待与恢复）、图 49（日终留证）

## 验收结果（10/10）

| 验收项 | 结果 | 证据 |
|---|---|---|
| ① checkpointer=postgres 且如实标注 restart_safe | ✅ | {"configured": "postgres", "dsn": "127.0.0.1:5432/platform", "restart_safe": true, "note": "状态落 PG，进程重启可恢复"} |
| ② 运行在 G1 挂起（状态已持久化） | ✅ | thread=wp7-drill-1789627203 approval=apv_d057efa83fd6 |
| ③ 归档记录了 SOP/触发方式/岗位 | ✅ | trigger=schedule role=product-agent checkpoint=postgres |
| ④ 重启后服务恢复 | ✅ |  |
| ⑤ 重启后仍是 postgres（未静默回退内存） | ✅ | {"configured": "postgres", "dsn": "127.0.0.1:5432/platform", "restart_safe": true, "note": "状态落 PG，进程重启可恢复"} |
| ⑥ kill -9 后线程状态仍在（停在 gate_g1，PRD 未丢） | ✅ | HTTP 200 next=['gate_g1'] prd_len=142 |
| ⑦ 从 checkpoint 恢复并继续执行（PRD 版本递增） | ✅ | status=waiting_approval prd_version=2 |
| ⑧ 数据未就绪时进入等待（不误判为故障） | ✅ | status=waiting_data |
| ⑨ 数据到齐后唤醒继续 | ✅ | status=waiting_approval |
| ⑩ 归档统计可用（含成本口径） | ✅ | runs=9 by_status={'waiting': 9} cost=0.0 unmetered_runs=4 |

## 关键证据

- **kill -9 后仍能继续**：进程被强杀并重启，`GET /runs/wp7-drill-1789627203` 仍返回 `waiting_approval`，
  随后 `/resume` 从断点继续（MemorySaver 下这里会 404）。
- 运行归档：`runs=9`｜状态分布 `{'waiting': 9}`｜
  成本 `$0.0`（未计量运行 4 次，如实标注不估算）。

## 结论

全部通过：状态可跨进程重启恢复，等待/唤醒与归档可用。
