# gitlab-bridge · GitLab 双向集成桥（T0.2）

> 方向一：GitLab webhook → 平台事件；方向二：平台 → GitLab API。
> 事件契约见 [contracts/events.md](../../contracts/events.md)。

## 快速开始

```bash
cd platform/services/gitlab-bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8001   # API 文档 http://localhost:8001/docs
pytest
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GITLAB_URL` | `https://gitlab.com` | 自建 GitLab 填实例地址 |
| `GITLAB_TOKEN` | 空 | PRIVATE-TOKEN（个人令牌或项目令牌，权限仅需 api + read_repository） |
| `WEBHOOK_SECRET` | 空 | webhook 校验密钥，**必填**（空则拒绝一切 webhook） |
| `PUBLISH_TARGETS` | 空 | 逗号分隔的事件扇出目标（如 n8n webhook）；空则用日志桩 |

## 在 GitLab 注册 webhook

1. 目标仓库 → Settings → Webhooks；
2. URL 填 `http://<本服务地址>/webhooks/gitlab`（本地调试可用 ngrok/内网穿透，或 GitLab Runner 所在网络可达的地址）；
3. Secret token 填 `WEBHOOK_SECRET` 的值；
4. 勾选事件：**Issues、Merge requests、Pipelines**；
5. 保存后在"Test"里触发一次 Issue 事件，观察日志应出现 `webhook accepted: ... gitlab.issue.opened`。

## 事件映射（→ contracts/events.md）

| GitLab 事件 | action/状态 | 平台事件 |
|---|---|---|
| Issue open | open | `gitlab.issue.opened` |
| Issue update/reopen/close | update/reopen/close | `gitlab.issue.updated`（含 changes） |
| MR open | open | `gitlab.mr.opened` |
| MR 合入 | merge | `gitlab.mr.merged`（含 merge_commit_sha） |
| Pipeline 终态 | success/failed/canceled/skipped | `gitlab.pipeline.finished` |
| 其他（note 等） | — | 忽略 |

**幂等**：事件 `trace_id = "gl-" + X-Gitlab-Event-UUID`，消费方按 (source, trace_id) 去重，天然抵御 GitLab 重试。

## 辅助端点（方向二，供演示流/Agent 调用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/gitlab/issues` | 创建 Issue（project_id 支持数字或 `group/project` 路径） |
| GET | `/gitlab/issues/{project_id}/{iid}` | 查询 Issue |
| POST | `/gitlab/mrs/notes` | MR 评论（AI 预审报告等） |
| GET | `/gitlab/pipelines/{project_id}/{pipeline_id}` | 查询 Pipeline |

## 本地快速验证

```bash
# 1) 起服务（用日志桩，无 GitLab 也能验证 webhook 链路）
WEBHOOK_SECRET=s3cret uvicorn app.main:app --port 8001

# 2) 模拟 GitLab 投递（另一终端）
curl -X POST http://localhost:8001/webhooks/gitlab \
  -H "X-Gitlab-Token: s3cret" -H "Content-Type: application/json" \
  -d '{"object_kind":"issue","project":{"id":42},"object_attributes":{"iid":1,"title":"t","action":"open","labels":[]}}'
```

服务日志应出现 `[LogPublisher] gitlab.issue.opened ...`。
