# 部署 · 回滚 · 故障处置（平台自身的运维手册）

> **这个目录不是知识库语料**。`platform/runbooks/*.md` 会被 `demo_alert.py` 灌进知识库
> 供运维域 SOP 做 RAG——那是**业务**运维手册。平台自己的部署/回滚文档放这里，
> 避免污染运维 Agent 的检索语料（把"平台怎么部署"喂给运维 Agent，它会在真实告警里检索到这些）。
>
> 目标读者：第一次独立操作平台的人。每条命令都应能直接粘贴。

## 0. 一次部署的完整顺序

```bash
# ① 生成并填写生产配置（密钥自动生成，人只填 9 项）
python3 platform/scripts/init_prod_env.py --out platform/.env.prod
python3 platform/scripts/init_prod_env.py --out platform/.env.prod --check   # 必须 exit 0
# ② 上线体检：治理开关不严就别往下走
python3 platform/scripts/check_deployment_readiness.py --env-file platform/.env.prod
# ③ 备份（迁移前必须有一份）
python3 platform/scripts/backup_restore.py backup --out platform/backups
# ④ 迁移数据库（**先迁移，后发服务**）
cd platform && services/approval-gateway/.venv/bin/python -m alembic upgrade head
# ⑤ 起服务（**叠加式**：基础文件提供 postgres/qdrant/litellm/n8n，prod 文件叠加平台服务）
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod up -d --build
# ⑥ 冒烟（见第 3 节）
```

**为什么先迁移后发服务**：迁移是向前兼容的（加列/加表），旧版本代码对新 schema 仍能跑；
反过来（先发新代码、库还没迁移）新代码会直接报缺列。涉及**删列/改语义**时必须两阶段发布：
先发"同时兼容新旧 schema"的代码，再迁移，最后清理。

## 1. 部署前检查清单

- [ ] `check_deployment_readiness.py --env-file platform/.env.prod` → **exit 0**
- [ ] `platform/.env.prod` 权限 600，且**没有**提交进仓库（`git status` 里不该出现）
- [ ] 备份刚跑过，且 `backup_restore.py drill` 最近一次是通过的
- [ ] `alembic current` 显示的版本 = 你要发布的代码期望的版本
- [ ] 变更窗口内有人能收到告警（`ALERT_NOTIFY_WEBHOOK` 已配、watchdog 容器在跑）

## 2. 部署

```bash
cd platform
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod"

$COMPOSE config -q                       # 先验证编排文件本身能解析（别等 up 才发现写错）
$COMPOSE up -d --build                   # 全量
$COMPOSE up -d --build task-center       # 单服务
$COMPOSE ps                              # 看 STATUS 里的 (healthy)
$COMPOSE logs -f --tail=100 approval-gateway
```

> 为什么是"叠加式"而不是只跑 `docker-compose.prod.yml`：基础文件里才有
> postgres / qdrant / litellm / n8n，prod 文件只叠加平台服务与生产开关。
> 单独跑 prod 文件会报 `depends on undefined service "postgres"`。
>
> 另外：服务定义里有 `env_file: [.env.prod]`，所以配置**必须**落在 `platform/.env.prod`
> 这个路径上——`--env-file` 指向别处只能满足变量插值，`config`/`up` 仍会报
> `env file ... not found`。

`(healthy)` 来自第 4 节加的 healthcheck——**容器活着不等于能用**，`/health` 会报
checkpointer 是否重启安全、知识库是否降级、治理姿态是否合规。

## 3. 冒烟（部署后 2 分钟）

```bash
# 服务全绿 + 治理姿态
python3 platform/scripts/watch_health.py --once

# 端到端探针（需要 SUT 端口可访问；生产按实际域名改 GW/TC/RT）
cd test-campaign
for p in im-callback gate-mode task-fsm reject-archive evidence; do bash probes.sh $p; done
for p in no-token with-token resume-bypass tools-selfdeclared alert-signature; do bash probes-authz.sh $p; done
```

判据：`watch_health.py` exit 0；探针全 `RESULT: PASS`。任一失败 → 走第 5 节回滚。

## 4. 平台自身的健康与告警

| 组件 | 作用 |
|---|---|
| 每个服务的 `/health` | 返回 `status` + `security`（档位/合规/未开开关）+ `checkpointer`（是否重启安全） |
| 每个服务的容器 healthcheck | `docker ps` 直接看到 `(healthy)` / `(unhealthy)` |
| `watchdog` 容器 | 每 60s 检查全部服务；**只在状态变化时**通知（避免刷屏被静音） |

watchdog 的两条通知通道：

1. `ALERT_NOTIFY_WEBHOOK`（飞书机器人）——**外部通道**，平台全挂时仍能发出去；
2. 平台自己的 `task-center /alerts/prometheus`——故障变成运维任务（吃自己的狗粮），
   但 task-center 也挂了时它会失败，**所以它不能是唯一通道**。

手工检查：

```bash
python3 platform/scripts/watch_health.py --once                  # 单次
python3 platform/scripts/watch_health.py --loop --interval 60     # 常驻（容器里就是这么跑的）
WATCH_TARGETS="approval-gateway=http://127.0.0.1:8001" python3 platform/scripts/watch_health.py --once
```

## 5. 回滚

**服务回滚**（代码问题、迁移兼容）：

```bash
cd platform
git log --oneline -5                                   # 找到上一个好版本
git checkout <好版本> -- services/                     # 或用镜像 tag 回退
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod \
  up -d --build approval-gateway
```

**数据库回滚**——先判断属于哪种：

| 情形 | 动作 |
|---|---|
| 迁移是加列/加表 | **不回滚**。新列对旧代码无害，回滚反而制造版本差；修完代码再前进 |
| 迁移改了/删了列，且已写入数据 | 用迁移前的备份恢复：`backup_restore.py restore --file <dump> --db platform_restored`，核对后再切换 |
| 迁移本身写错了（还没发新代码） | `alembic downgrade -1`（**先备份**，downgrade 会删数据） |

**恢复演练**（每季度至少一次，也建议在每次大改动前跑）：

```bash
python3 platform/scripts/backup_restore.py drill --dbs platform,litellm
```

判据：报告里每个库"源/恢复库行数一致"，且 exit 0。

## 6. 常见故障处置

| 症状 | 先看什么 | 常见原因 |
|---|---|---|
| 服务起不来，日志 `CRITICAL ... 拒绝启动` | `PLATFORM_ENV` 与凭据 | prod 档位要求服务令牌，但 `SERVICE_AUTH_TOKEN` 没配 |
| 接口一律 401 | 调用方有没有带 `X-Service-Token` | 开了令牌模式，但调用方（含脚本/探针）没带；服务间调用见 `outbound_headers()` |
| 审批台白屏 | 浏览器控制台 + `curl /app.js` | 静态资源没被豁免（应自动豁免 `static/` 目录内容）；或 CSP 拦了内联脚本 |
| 飞书卡片按钮点了没反应 | `approval-gateway` 日志 | `IM_CALLBACK_SIGNING_KEY` / `FEISHU_VERIFY_TOKEN` 没配 → 回调 503 |
| 告警进不来 | `task-center` 日志 | `ALERT_WEBHOOK_SECRET` 与 Alertmanager 侧不一致（无凭据一律 401） |
| 审批一直 pending 不超时处置 | sweeper 是否在跑 | `SWEEP_INTERVAL_SECONDS=0` 且没人调 `POST /admin/sweep` |
| 重启后等待中的运行消失 | `/health` 的 `checkpointer.restart_safe` | `CHECKPOINTER=memory`，应设 `postgres` |
| 迁移失败卡在半路 | `alembic current` + 日志 | 迁移脚本有数据依赖；**先备份**，必要时用备份恢复后重来 |

## 7. 值班与升级（试点期最小约定）

- **谁收到告警**：`ALERT_NOTIFY_WEBHOOK` 指向的群；试点期指定 1 名 primary、1 名 backup。
- **多久响应**：P1（服务不可用 / 审批台打不开）15 分钟内确认；姿态类告警当日处理。
- **升级路径**：primary 15 分钟未响应 → backup → 平台负责人。
- **什么必须停下来**：出现"审批被绕过"或"审计留痕缺失"的任何迹象 → 立即停止试点，
  保留现场（备份 + 日志），因为这两件事一旦发生，平台的立身之本就没了。

## 8. 本手册未覆盖（仍待补）

- 公网入口（Caddy + oauth2-proxy）的真实部署与验证——见 `22-上线前最小路径与验收记录.md` 第 6 节；
- 性能基线与 soak 结论；
- 备份的异地存放与保留策略（脚本只负责生成与验证恢复）；
- 多副本/水平扩展下的行为（当前行锁与唯一键只在单库单实例上验证过）。
