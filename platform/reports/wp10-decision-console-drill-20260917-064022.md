# WP10 驾驶舱演练报告

- 执行时间：2026-09-17T06:40:22.101812+00:00
- 队列快照：`{'approvals': 1, 'gate_blocks': 1, 'run_waits': 0, 'total': 2}`｜上游错误：`['运行归档不可用']`
- 误拦统计：`{'blocked_total': 1, 'judged': 1, 'pending_feedback': 0, 'coverage': 1.0, 'false_positive': 1, 'false_positive_rate': 1.0, 'reliable': False, 'unreliable_reasons': ['已反馈样本 1 < 门槛 20'], 'threshold': 0.1, 'recommended_action': '样本不足：继续收集反馈，暂不据此调整闸门宽严', 'by_code': {'E1': {'judged': 1, 'false_positive': 1}}}`
- 对应课程：图 67/68（驾驶舱显示进度与待决策事项、点击直达上下文）

## 验收结果（8/8）

| 验收项 | 结果 | 证据 |
|---|---|---|
| ① 队列聚合待审审批与闸门阻断 | ✅ | counts={'approvals': 1, 'gate_blocks': 1, 'run_waits': 0, 'total': 2} |
| ② 每条带「为什么卡住 + 动作 + 深链」 | ✅ | 审批项动作=['approve', 'reject', 'revise']；闸门项动作=['feedback_valid', 'feedback_false_positive'] |
| ③ 队列展示来源链与风险（决策依据可见） | ✅ | sources=1 risk=L1 |
| ④ 上游不可用时如实报告（运行归档故意关闭） | ✅ | upstream_errors=['运行归档不可用'] |
| ⑤ 驾驶舱决策提交成功 | ✅ | decision=approve by=值班经理 |
| ⑥ 决策在审批网关留痕（未绕过审计） | ✅ | status=approved decisions=1 |
| ⑦ 误拦反馈可提交并计入统计 | ✅ | 误拦率 None → 1.0（已反馈 1/1） |
| ⑧ 面板渲染待决策队列与误拦率 | ✅ | 面板含待决策区块与反馈按钮 |

## 结论

全部通过：待决策队列可用、决策经网关留痕、误拦反馈形成闭环（误拦率可计算）。
