# WP1 需求来源链演练报告

- 执行时间：2026-09-17T06:39:50.225676+00:00
- 需求原文：列表加个导出吧，支持常用格式就行；字段跟列表页保持一致（主要字段等）；权限按常规处理，运营也能看；数据量大的时候优化一下。
- 澄清方式：规则检测（确定性，CLARIFY_USE_LLM=0）
- task_ref：`wp1-drill-1789627189`｜审批单：`apv_c5608d5291a2`

## 验收结果（12/12）

| 验收项 | 结果 | 证据 |
|---|---|---|
| 两个服务健康 | ✅ |  |
| ① 产品域流程在 G1 挂起 | ✅ | status=waiting_approval approval=apv_c5608d5291a2 |
| ② 澄清节点发现模糊表述 | ✅ | 命中 5 处：['常用格式', '主要字段', '按常规', '数据量大', '优化一下'] |
| ③ 每条待确认问题都挂出处 | ✅ | 5/5 条带出处 |
| ④ 明示默认值清单已生成 | ✅ | 6 项（含时区/空值/精度/上限） |
| ⑤ 来源链随 G1 审批落库 | ✅ | 落库 3 条（voice/chat/excel） |
| ⑥ 反查到语音出处与时间戳 | ✅ | feishu://chat/oc_drill/msg/om_voice_214 @ {'start_ms': 41200, 'end_ms': 47000} |
| ⑦ 反查到「按月分仓」原话 | ✅ | quote=我们对账是按月分仓看的，不是按天。 |
| ⑧ 出处带确认人（谁说的） | ✅ | confirmed_by=李经理 |
| ⑨ 按载体汇总正确 | ✅ | {"voice": 1, "chat": 1, "excel": 1} |
| ⑩ 无缺出处的 Gate | ✅ | gates_without_sources=[] |
| ⑪ 跨产品线查不到（租户隔离） | ✅ | HTTP 404 |

## 反查链路（产物 → 出处）

```
需求「报表导出」→ task_ref wp1-drill-1789627189
  └─ 关键口径：我们对账是按月分仓看的，不是按天。
       kind=voice ref=feishu://chat/oc_drill/msg/om_voice_214
       locator={"start_ms": 41200, "end_ms": 47000}
       confirmed_by=李经理
  └─ 素材：chat feishu://chat/oc_drill/msg/om_chat_001｜excel feishu://drive/file/月度对账.xlsx
```

## 结论

全部通过：来源链可从产物反查到语音时间戳与原话。
