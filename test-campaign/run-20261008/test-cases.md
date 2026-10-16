# 测试卡清单（camp-pst-20261008）

共 71 张卡；优先级分布 P0=42、P1=27、P2=2。

| 卡 | 优先级 | 层 | 标题 | 执行 | 期望 |
|---|---|---|---|---|---|
| REG-001 | P0 | api | IM 回调必须验签（BUG-001） | `bash test-campaign/probes.sh im-callback` | im-callback 探针输出 RESULT: PASS |
| REG-002 | P0 | api | 跨租户读取被拒（BUG-002 同源） | `bash test-campaign/probes.sh cross-tenant-read` | cross-tenant-read 探针输出 RESULT: PASS |
| REG-003 | P1 | api | resubmit 遵守租户边界（BUG-002） | `bash test-campaign/probes.sh resubmit-tenant` | resubmit-tenant 探针输出 RESULT: PASS |
| REG-004 | P0 | api | 闸门模式不可被调用方覆盖（BUG-003） | `bash test-campaign/probes.sh gate-mode` | gate-mode 探针输出 RESULT: PASS |
| REG-005 | P0 | api | 每次闸门判定都留痕（BUG-014） | `bash test-campaign/probes.sh gate-persist` | gate-persist 探针输出 RESULT: PASS |
| REG-006 | P0 | api | 超时处置取服务端信任等级（BUG-004） | `bash test-campaign/probes.sh timeout-auto` | timeout-auto 探针输出 RESULT: PASS |
| REG-007 | P0 | api | 等待审批不得被无据流转（BUG-005） | `bash test-campaign/probes.sh task-fsm` | task-fsm 探针输出 RESULT: PASS |
| REG-008 | P1 | api | 非法流转 409（FSM-002） | `bash test-campaign/probes.sh task-fsm-illegal` | task-fsm-illegal 探针输出 RESULT: PASS |
| REG-009 | P0 | api | 任务详情租户隔离（BUG-006） | `bash test-campaign/probes.sh task-tenant` | task-tenant 探针输出 RESULT: PASS |
| REG-010 | P1 | api | 重复外部引用不 500（BUG-015） | `bash test-campaign/probes.sh task-dup-ref` | task-dup-ref 探针输出 RESULT: PASS |
| REG-011 | P0 | api | 被否决的运行归档可见（BUG-012） | `bash test-campaign/probes.sh reject-archive` | reject-archive 探针输出 RESULT: PASS |
| REG-012 | P0 | api | 等待人审不得被 /wake 吞掉（BUG-007） | `bash test-campaign/probes.sh wake-guard` | wake-guard 探针输出 RESULT: PASS |
| REG-013 | P1 | api | 启发式挖掘方向正确（BUG-008） | `bash test-campaign/probes.sh heuristic` | heuristic 探针输出 RESULT: PASS |
| REG-014 | P1 | api | α 挖掘自洽（BUG-009） | `bash test-campaign/probes.sh alpha` | alpha 探针输出 RESULT: PASS |
| REG-015 | P1 | api | DFG 一致性检出异常（BUG-010） | `bash test-campaign/probes.sh dfg-conformance` | dfg-conformance 探针输出 RESULT: PASS |
| REG-016 | P1 | api | LLM 畸形输出回退规则（BUG-011） | `bash test-campaign/probes.sh llm-fallback` | llm-fallback 探针输出 RESULT: PASS |
| REG-017 | P1 | api | 证据上传与哈希复验（EVD） | `bash test-campaign/probes.sh evidence` | evidence 探针输出 RESULT: PASS |
| REG-018 | P1 | api | 健康如实报告持久化形态（WP7） | `bash test-campaign/probes.sh health-checkpointer` | health-checkpointer 探针输出 RESULT: PASS |
| SEC-101 | P0 | security | 写接口鉴权覆盖率为 100%（清点） | `python3 platform/scripts/audit_endpoint_auth.py --strict` | audit 输出“无鉴权 0 个” |
| SEC-102 | P0 | security | 无令牌的读写请求被拒，健康检查豁免 | `bash probes-authz.sh no-token` | 探针输出 RESULT: PASS |
| SEC-103 | P0 | security | 带令牌的审批与决策链路仍可用 | `bash probes-authz.sh with-token` | 探针输出 RESULT: PASS |
| SEC-104 | P0 | security | 自述决策不得恢复人审挂起 | `bash probes-authz.sh resume-bypass` | 探针输出 RESULT: PASS |
| SEC-105 | P0 | security | 工具调用不接受请求体自述岗位/免人审 | `bash probes-authz.sh tools-selfdeclared` | 探针输出 RESULT: PASS |
| SEC-106 | P1 | security | 告警 webhook 验签（无凭据/错密钥拒绝） | `bash probes-authz.sh alert-signature` | 探针输出 RESULT: PASS |
| SEC-107 | P0 | security | 令牌模式下审批台静态资源可达、业务接口仍需令牌 | `for f in '' index.html app.js app.css; do curl -s -o /dev/null -w "$f=%{http_code} " http:` | index/app.js/app.css 均 200 且 /approvals 401 |
| SEC-108 | P0 | security | 直连服务伪造入口身份头必须无效 | `curl -s -o /dev/null -w 'status=%{http_code}\n' -H 'X-Platform-User: attacker' http://127.` | 伪造 X-Platform-User 直连驾驶舱应得 401 |
| SEC-109 | P1 | security | GitLab webhook 未签名被拒、正确签名放行 | `curl -s -o /dev/null -w 'no_sig=%{http_code} ' -X POST http://127.0.0.1:18006/webhooks/git` | 无签名 401、带 X-Gitlab-Token 200 |
| CONC-201 | P0 | stability | 30 并发决策：恰好一个成功、一条决策 | `STRESS_ROUNDS=6 STRESS_N=30 bash probes-concurrency.sh decide-race-stress` | 命令输出 RESULT: PASS |
| CONC-202 | P0 | stability | 30 并发流转：恰好一个成功、一条时间线 | `STRESS_ROUNDS=6 STRESS_N=30 bash probes-concurrency.sh task-race-stress` | 命令输出 RESULT: PASS |
| CONC-203 | P0 | stability | 人工决策与超时扫描并发：不双决策 | `bash probes-concurrency.sh decide-vs-sweep 2>&1 | iconv -f utf-8 -t utf-8 -c` | 探针输出 RESULT: PASS |
| CONC-204 | P0 | stability | IM 回调与 Web 决策并发：互斥 | `bash probes-concurrency.sh im-vs-decide` | 探针输出 RESULT: PASS |
| CONC-205 | P1 | stability | 并发上传证据不丢件 | `bash probes-concurrency.sh evidence-race 2>&1 | iconv -f utf-8 -t utf-8 -c` | 探针输出 RESULT: PASS |
| UI-301 | P0 | e2e | 审批台加载：静态外壳可见、无控制台报错 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 步骤全通过且页面无控制台错误 |
| UI-302 | P0 | e2e | 待审列表必须显示真实待办（不得静默空） | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 列表渲染出至少一条待审单据 |
| UI-303 | P0 | e2e | 详情渲染三要素与动作按钮 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 详情出现①②③三要素与批准/拒绝按钮 |
| UI-304 | P0 | e2e | XSS 载荷只作为文本出现，不生成元素、不执行 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 无 img/script 元素、无脚本执行、无控制台错误 |
| UI-305 | P1 | e2e | 审批台可访问性：按钮有可访问名 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | accessibility tree 中存在名为“批准”“拒绝”的按钮 |
| UI-306 | P1 | e2e | 视觉基线比对（审批台首屏） | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 与基线差异比例 ≤ 2% |
| UI-309 | P0 | e2e | 在界面上完成一次决策（浏览器端到端） | `python3 ui/seed.py --marker camp1008 >/dev/null && python3 /Users/edy/.dsh/skills/full-sys` | 输入令牌后点击批准，页面提示已提交（原生 prompt 由测试替换） |
| UI-308 | P0 | e2e | 无令牌时必须显示鉴权错误，不得伪装成空审批队列 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 未登录时页面必须显示错误提示与“审批列表加载失败”，且不得出现空态文案 |
| UI-307 | P0 | e2e | 视觉比对负向控制：注入样式缺陷必须被发现 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/cons` | 改动基线样式后差异比例必须超过阈值 |
| UI-311 | P0 | e2e | 驾驶舱经入口代理可读并渲染队列 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/dash` | 页面出现待决策队列与误拦率文案 |
| UI-312 | P0 | e2e | 驾驶舱队列渲染真实事项 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/dash` | 表格存在且含 approval/gate_block/空态说明之一 |
| UI-313 | P2 | e2e | 驾驶舱可访问性树可用 | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/dash` | 存在 role=table |
| UI-314 | P2 | e2e | 视觉基线比对（驾驶舱首屏） | `python3 /Users/edy/.dsh/skills/full-system-testing/scripts/browser_test.py --suite ui/dash` | 与基线差异比例 ≤ 5% |
| DATA-401 | P0 | data | 迁移：空库可建 + 模型零漂移 + 可回滚 | `platform/services/approval-gateway/.venv/bin/python platform/scripts/check_migrations.py -` | 命令输出“迁移体检通过” |
| DATA-402 | P0 | data | 迁移与模型建表结果一致（零漂移） | `python3 flows/schema_compare.py` | 命令输出“迁移与模型一致” |
| DATA-405 | P1 | data | 运行归档与平台库同库（度量才看得到运行） | `python3 flows/runs_db_check.py` | 运行时库应包含 agent_runs（或明确声明归档另存并在度量中体现） |
| DATA-403 | P0 | data | 备份可恢复（恢复演练） | `python3 platform/scripts/backup_restore.py drill --dbs platform` | 命令输出“恢复演练通过” |
| DATA-404 | P1 | data | 证据哈希复验：篡改可被发现 | `bash test-campaign/probes.sh evidence` | 篡改后 verify 报告 broken ≥ 1 |
| OPS-501 | P1 | other | 看门狗判级/去重/通知自检 | `python3 platform/scripts/watch_health.py --selftest` | 命令输出“自检通过” |
| OPS-502 | P1 | other | 看门狗对 7 服务判定为健康 | `WATCH_TARGETS='approval-gateway=http://127.0.0.1:18001,agent-runtime=http://127.0.0.1:1800` | 输出“平台健康：ok” |
| OPS-503 | P1 | other | 7 个服务都暴露治理姿态 | `for p in 18001 18002 18003 18004 18005 18006 18007; do curl -s -m 5 -H 'X-Service-Token: p` | 7 个 /health 都含 security.compliant 字段 |
| OPS-504 | P0 | other | 生产档位缺凭据必须拒绝启动 | `cd platform/services/task-center && PLATFORM_ENV=prod SERVICE_AUTH_MODE= SERVICE_AUTH_TOKE` | 进程非 0 退出且日志含“拒绝启动” |
| DEP-601 | P0 | other | 上线体检：prod 模板必须可上线 | `python3 scripts/check_deployment_readiness.py --env-file .env.prod.example` | 命令输出“上线就绪” |
| DEP-602 | P1 | other | 生产配置生成器自检 | `python3 scripts/init_prod_env.py --selftest` | 命令输出“自检通过” |
| DEP-603 | P0 | other | 生产编排可解析（叠加式） | `cp .env.prod.example .env.prod && docker compose -f docker-compose.yml -f docker-compose.p` | docker compose config 退出 0 |
| DEP-604 | P1 | other | 鉴权模块 7 份副本无漂移 | `python3 scripts/sync_service_auth.py --check` | 命令输出 OK |
| INT-801 | P0 | integration | 告警 → 建任务 → 自动拉起运维 SOP | `curl -s -m 20 -X POST http://127.0.0.1:18004/alerts/prometheus -H 'Content-Type: applicati` | 带密钥告警返回 200 且建出 ops-domain 任务 |
| INT-802 | P0 | integration | 事件投递：approval.decided → task-center /events | `T=apv_evt_$(date +%s); curl -s -m 10 -X POST http://127.0.0.1:18004/events -H 'Content-Typ` | 投递后 task_events 出现审批决策事件 |
| INT-803 | P1 | integration | 受管知识：项目授权下可入库与检索 | `python3 flows/kb_check.py` | 注册项目版本后入库成功、检索命中且来源在授权范围内 |
| INT-804 | P1 | integration | 流程挖掘提案 → G7 审批单创建 | `bash test-campaign/probes.sh heuristic` | 提案接口产出提案且 submit 建出审批 |
| INT-805 | P0 | integration | 驾驶舱决策代理透传身份（不绕过审计） | `curl -s -o /dev/null -w 'status=%{http_code}\n' -X POST http://127.0.0.1:18005/console/dec` | 无身份的 /console/decide 被拒（401/403/502），而不是 200 |
| FLOW-801 | P0 | flow | 产品域端到端：需求→PRD→G1 人审→否决→归档 | `python3 flows/run_flow.py --domain product` | 挂起 G1、人审决策被接受、否决反映到归档 |
| FLOW-807 | P1 | flow | 研发域端到端：issue→方案→G2 人审决策 | `python3 flows/run_flow.py --domain dev` | 挂起 G2 且人审决策被接受（编码/MR 段见声明缺口） |
| FLOW-802 | P1 | flow | 测试域端到端：用例→执行→G4 人审 | `python3 flows/run_flow.py --domain test` | 挂起人审并可恢复（本环境无仓库令牌，执行段见声明缺口） |
| FLOW-803 | P0 | flow | 运维域端到端：告警→SOP→G5 人审决策被接受 | `python3 flows/run_flow.py --domain ops` | 挂起 G5 且人审决策落库 |
| FLOW-804 | P1 | flow | 平台域端到端：挖掘→提案→G7 审批；未批不得应用 | `python3 flows/run_flow.py --domain platform` | 产出提案、建出 G7 审批单、未获批应用被拒 |
| FLOW-805 | P0 | flow | 决策闭环（浏览器）：审批人在界面上完成一次决策 | `python3 flows/run_flow.py --domain ui` | 界面可完成决策且无失败提示 |
| FLOW-806 | P0 | flow | 运维域契约：task_ref 与告警 id 不一致时不得死锁 | `python3 flows/run_flow.py --domain ops-taskref-mismatch` | 审批单 task_ref 与运行 task_ref 不一致时，恢复仍应可用 |
| PROV-901 | P1 | api | 需求来源链可反查（谁在哪句话提的） | `python3 flows/prov_check.py` | 反查返回带 quote/confirmed_by 的来源条目且 has_provenance=true |
