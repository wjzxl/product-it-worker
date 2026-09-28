# 平台库迁移（Alembic）

平台库（PostgreSQL `platform`）由**三个服务共享**：approval-gateway（审批/闸门/证据/信任）、
task-center（任务/时间线/事件收件箱）、agent-runtime（运行归档）。因此迁移目录放在**仓库级**
而不是某个服务内——schema 是平台资产，不属于某一个服务。

## 为什么需要它（此前的问题）

此前靠 `Base.metadata.create_all` + 各服务里的 `_ensure_xxx_column()` 临时补列：

- `create_all` 只会建"缺的表"，**改不了已有表的列**——线上加字段要靠人记得手写 ALTER；
- 没有版本记录 ⇒ 无法回答"这个库现在什么版本""升级到哪一步失败""怎么回滚"；
- 多版本滚动升级/回滚是上线的硬前提，而这条路径此前**不存在**。

## 常用命令

用 `approval-gateway` 的 venv（它带 alembic）：

```bash
cd platform

# 升级到最新（空库 = 建全部平台表；既有库 = 增量升级）
services/approval-gateway/.venv/bin/python -m alembic upgrade head

# 查当前版本 / 历史
services/approval-gateway/.venv/bin/python -m alembic current
services/approval-gateway/.venv/bin/python -m alembic history

# 改完模型后生成迁移（务必人工审阅自动生成的脚本！）
services/approval-gateway/.venv/bin/python -m alembic revision --autogenerate -m "说明"

# 回滚一步 / 回到空库
services/approval-gateway/.venv/bin/python -m alembic downgrade -1
services/approval-gateway/.venv/bin/python -m alembic downgrade base
```

连接串优先级：`ALEMBIC_DATABASE_URL` > `DATABASE_URL` > `alembic.ini` 的 `sqlalchemy.url`。

## 既有库怎么纳入管理

已经存在的库（schema 与 baseline 一致，只是没有版本记录）**不要**跑 `upgrade`，而是打标：

```bash
ALEMBIC_DATABASE_URL='postgresql+psycopg://platform:platform@127.0.0.1:5432/platform' \
  services/approval-gateway/.venv/bin/python -m alembic stamp head
```

本仓库的本地平台库已按此打标（`9c049b104c58`），数据未改动。

## 管辖边界（比对时别把别人的表算成漂移）

| 表 | 谁管 |
|---|---|
| `approval_requests` `approval_decisions` `gate_checks` `evidence_artifacts` `trust_state` | 本迁移（approval-gateway 模型） |
| `tasks` `task_events` `event_inbox` | 本迁移（task-center 模型） |
| `agent_runs` | 本迁移（agent-runtime 模型） |
| `checkpoints` `checkpoint_blobs` `checkpoint_writes` `checkpoint_migrations` | **LangGraph** 的 `PostgresSaver.setup()` 自管 |
| `litellm` 库全部表 | **LiteLLM** 自管 |

## 体检（CI 会跑）

```bash
services/approval-gateway/.venv/bin/python scripts/check_migrations.py --selftest
```

三件事：① 空库 `upgrade head` 成功；② **模型与迁移零漂移**（改了模型没补迁移会红）；
③ `downgrade base` 能干净回滚。自检里故意加一张只存在于模型侧的表，比对器必须**恰好**
报出这一处——否则说明体检本身是假绿的。

## 加字段的标准动作

1. 改服务里的模型（如 `approval-gateway/app/models.py`）；
2. `alembic revision --autogenerate -m "add xxx"`，**人工审阅**脚本（自动生成会漏数据迁移）；
3. 若涉及存量数据（补默认值、回填），在同一个 revision 里写 `op.execute(...)`；
4. `alembic upgrade head` 验证；跑 `scripts/check_migrations.py` 确认零漂移；
5. 上线顺序：**先迁移，后发服务**（旧代码对新 schema 要能跑；必要时应做两阶段发布）。
