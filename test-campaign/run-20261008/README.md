# camp-pst-20261008 · 第 2 次完整战役（发布判定复测 + 上轮缺陷修复复验）

对同一 revision 的**两轮**完整战役：

| 轮次 | 产物 | 结论 |
|---|---|---|
| 修复前 | `before-fix/results.json`、`before-fix/test-report/` | advisory `BLOCKED`、人工 `NO_RELEASE`；11 张卡失败、7 个缺陷（2 CRITICAL） |
| 修复后 | `results.json`、`test-report/` | advisory 见报告首屏；上轮 7 条缺陷 6 FIXED / 1 OPEN，另发现 3 条新缺陷 |

## 一行复现

```bash
# 1) 一次性环境（7 服务 + PG pst_sut + Qdrant；会重建 pst_sut）
bash test-campaign/start-sut.sh --backend postgres
# 2) 入口代理夹具（模拟 Caddy+SSO：剥离伪造头、注入 X-Platform-User 与独立代理密钥）
python3 test-campaign/run-20261008/ui/proxy.py --listen 18015 --target http://127.0.0.1:18005 \
    --user campaign-ui --proxy-token pst-sut-proxy-token &
# 3) UI 前置数据（含一条 XSS 载荷）
python3 test-campaign/run-20261008/ui/seed.py --marker camp1008
# 4) 跑计划 → 出报告
cd test-campaign/run-20261008
python3 /Users/edy/.dsh/skills/full-system-testing/scripts/run_tests.py run \
    --plan test-plan.json --results results.json --evidence-dir evidence
python3 /Users/edy/.dsh/skills/full-system-testing/scripts/run_tests.py report \
    --input results.json --out-dir test-report
```

> 注意：`run --card <ids>` 是**子集运行**，会把未选中的卡重置为未执行；要出发布结论必须整跑一次。

## 本轮契约变化（复现时必须知道）

1. **人类动作需要人类凭据**：决策/工程闸门/证据上传要求 `Authorization: Bearer <个人令牌>`；
   服务间调用用 `X-Service-Token`。**两者不可同时携带**（会判 `invalid-personal-bearer`，见 DEF-008）。
2. **主体授权范围**：请求头 `X-Tenant` 必须在主体的授权租户内，否则 403
   （SUT 通过 `SERVICE_TENANTS` 授予战役用租户）。
3. **可信代理需独立密钥**：`TRUST_PROXY_AUTH=1` 必须配 `PROXY_AUTH_TOKEN`，请求带 `X-Proxy-Token`。
4. **受管知识访问**：入库/检索要带 `project_id` + `project_version` + `repository_revision`，
   且项目版本的作用域是**规范前缀**（`runbooks`，不是 `runbooks/**`）。
5. **运行按 (tenant, sop, task_ref) 幂等**：重复调用返回既有运行，夹具需用唯一引用。

## 产物

| 文件 | 内容 |
|---|---|
| `system-model.json` / `test-plan.json` | 20 特征 / 6 业务流 / 22 需求 / 71 张卡 |
| `results.json` | **唯一事实来源**：每张卡判定、证据哈希、10 条缺陷、性能场景 |
| `test-report/` | 生成器渲染的报告 + 覆盖矩阵 + defects.csv |
| `defects.md` | 缺陷台账（含"修复前→修复后"复验记录） |
| `evidence/` | 每张卡的命令、退出码、stdout/stderr 原文；UI 截图与视觉差异图（截图不入库） |
| `before-fix/` | 修复前的 bundle 与报告（保留原始失败，不覆盖） |
| `flows/`、`ui/` | 业务流脚本、来源链/知识库/schema 校验脚本、浏览器套件与代理夹具 |

## 未覆盖（诚实清单）

真实模型质量与提示注入、真实 GitLab 写入与飞书推送、公网 SSO 入口、多副本与外部 PG、
浏览器兼容性矩阵、i18n/时区、长稳 soak（>30min）。详见 `results.json` 的 `verdict.not_tested`。
