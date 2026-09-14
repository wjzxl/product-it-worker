# 11 · Phase 2 收尾实施记录（Runbook RAG / 告警接入 / 沙箱加固 / 度量面板）

> 收尾三项：① Runbook 接入知识库 RAG + 真实监控告警接入；② 编码沙箱容器隔离加固；③ 三域指标度量面板 + Phase 2 正式验收报告。

## ① Runbook RAG + 真实监控告警接入 ✅（本轮完成）

### 知识库 RAG（Qwen 向量）
- 网关新增 `qwen-embed`（DashScope `text-embedding-v3`，1024 维），知识库 `EMBEDDING_MODEL=qwen-embed`；
- Runbook 文档库：`platform/runbooks/`（导出性能 / CPU 高负载 / 5xx 错误率），入库脚本 `scripts/seed_runbooks.py`；
- 运维域诊断节点接入检索：`diagnose` 先按告警标题+描述检索 Runbook（limit 3），命中文本带来源注入诊断 prompt；
- **引用留痕**：命中来源进入 G5 审批依据（evidence "Runbook 来源: …"）与运行状态 `runbook_sources`。

### Prometheus 告警真实接入
- task-center 新增 `POST /alerts/prometheus`（Alertmanager webhook 载荷）：
  - firing 告警 → 建任务（`external_ref=alert-<fingerprint>-<ts>`，sop=运维域）
  - **自动触发运维域 SOP**（`AGENT_RUNTIME_URL` + `OPS_PROJECT_ID`），告警 labels/annotations 归一化为 alert/context；
  - resolved 告警忽略；发布 `task.created` 事件。
- 真实监控接入方式：Alertmanager `receivers.webhook_configs.url = http://<平台>:8002/alerts/prometheus`。

### 真实验证（`scripts/demo_alert.py --gateway --pg`，Qwen 驱动）—— **10/10 通过**
| 检查 | 结果 |
|---|---|
| 五个服务健康（含知识库） | ✅ |
| Runbook 入库（Qwen 向量） | ✅ 3 篇 |
| Runbook 检索（带来源） | ✅ top=export-performance.md score=0.741 |
| Prometheus 告警接入 → 建任务 | ✅ |
| **自动触发运维域 SOP → G5 挂起** | ✅ |
| **诊断基于知识库 Runbook（引用留痕）** | ✅ sources=[export-performance.md, http-5xx.md] |
| G5 审批依据含 Runbook 来源 | ✅ type=fix_code |
| 变更执行（真实修复 MR 合入） | ✅ **MR !16 merged**（main 已含 365 天范围校验） |
| 复盘 → G6 → 闭环 | ✅ |
| 飞书卡片推送 | ✅ 0 失败 |

**里程碑**：RAG 提供的 Runbook（含"保持返回值类型不变"约束）+ 文件全文上下文，使 **AI 外科手术式修复首次真实成功**（此前 DeepSeek/Qwen 均失败于此）。

## ② 编码沙箱容器隔离加固 ✅（本轮完成）

### 沙箱镜像（`platform/sandbox/`）
- `Dockerfile`：`python:3.9-slim` + git + pytest 预装（执行期无需联网安装）；
- `README.md`：隔离要点与用法。

### 沙箱执行器（`app/sandbox.py`）
| 模式 | 说明 |
|---|---|
| `host`（默认） | 宿主直接执行（本地无 docker 时兼容） |
| `docker` | 在 `platform-sandbox` 镜像内执行：**`--network none`（执行期无网络）**、`--memory 1g`、`--cpus 1`、`--pids-limit 256`、**以宿主 uid 运行**（产物不归 root）、`--rm` 用后即毁 |

- 接入点：`coder._run_tests`（研发域质量门禁）与 `tester._pytest`（测试域分层执行）统一经 `runner`；
- 环境变量：`SANDBOX_MODE=docker`、`SANDBOX_IMAGE=platform-sandbox:latest`（docker 不可用自动回退 host 并告警）。

### 验证证据
| 验证 | 结果 |
|---|---|
| 沙箱单测（`tests/test_sandbox.py`，4 项） | ✅ host 模式、桩执行器注入（coder 必经沙箱）、**docker 隔离实测** |
| 隔离实测（`test_docker_sandbox_isolation`） | ✅ 容器内 `/.dockerenv` 存在（`IN_CONTAINER True`）+ `NET_BLOCKED`（无网络）+ 容器内 pytest 1 passed |
| 真实流水线（`demo_phase2.py` + `SANDBOX_MODE=docker`，Qwen） | ✅ 7/7 通过；agent-runtime 日志：`沙箱模式：docker（镜像 platform-sandbox:latest）` |

**诚实标注**：git clone/push 仍由宿主执行（需 token 与网络）；**AI 生成的代码与测试用例的执行（风险最高环节）已全部在无网络容器内完成**。

## ③ 三域指标度量面板 + Phase 2 验收报告（待实施）

