# 15 · Phase 3 扩展实施记录（应用动作 / 流程挖掘算法 / 多产品线与模型评估）

> 三项扩展：① 扩展 SOP 自进化应用动作（Prompt/知识库）；② 标准流程挖掘算法；③ 多产品线推广 + 私有化/多模型评估。

## ① 扩展应用动作：Prompt 版本化 + 知识库自动优化 ✅（已完成）

### Prompt 注册中心（`platform/prompts/`）
- `registry.yaml`（active 版本表）+ `<key>.v<N>.md`（各 Agent Prompt 全文，初始 v1 = 既有内置 Prompt）；
- Agent 键：`coder_agent` / `tester_agent` / `ops_diagnose_agent` / `prd_agent` / `tech_design_agent`；
- agent-runtime `app/prompts.py`：读取顺序 `PROMPTS_DIR` → registry active 版本 → 文件内容；缺失回退内置默认；
  **mtime 热更新**（应用后无需重启即生效）；
- 已接线：编码 Agent（coder）、测试 Agent（tester）、运维诊断 Agent（diagnose）的 system prompt 均由注册中心提供；
- 新增 `GET /prompts`：返回 active 版本 + 内容指纹（长度/sha256 前 16 位），用于版本溯源与生效验证。

### 新应用动作（process-mining `apply.py`）
| 动作 | 语义 | 校验 |
|---|---|---|
| `prompt_version` | 写入 `<key>.v<N+1>.md` 并切换 active（Agent 热更新生效） | Prompt 键必须在注册表；内容 50~8000 字符 |
| `knowledge_ingest` | 把新规范/经验写入知识库（`source=<target>/auto-<ts>.md`） | 来源前缀正则；内容 20~20000 字符 |

### 真实验证（`scripts/apply_optimizations.py`）—— **8/8 通过**
| 检查 | 结果 |
|---|---|
| ① 初始版本 | ✅ coder_agent=v1 |
| ② Prompt 新版本写入并切换 | ✅ v1 → v2 |
| ③ **热更新生效（无需重启）** | ✅ 指纹变化：v1(len=195, sha=ad0feaf8) → v2(len=270, sha=9e33f979) |
| ④ / ⑦ GitLab 改进项登记 | ✅ issue #51、#52 |
| ⑤ 知识自动入库 | ✅ `runbooks/auto-20260914-091627.md`（1 块） |
| ⑥ 运维域检索命中新知识 | ✅ top=runbooks/auto-20260914-091627.md（命中自动入库 2 条） |

### 测试
agent-runtime 34 项（新增 prompts 5：加载/热更新/缺失回退/坏配置容错）；process-mining 25 项（新增应用动作 6：Prompt 版本切换/未知键/长度校验/入库请求/非法来源/提案可应用标记）。
