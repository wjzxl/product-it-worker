# 模型评估报告（离线金标集 + 沙箱判分）

- 评估时间：2026-09-14T10:05:44.734695+00:00 → 2026-09-14T10:05:44.988494+00:00
- 评估端点：`http://127.0.0.1:4000/v1`（`EVAL_BASE_URL` 可指向私有化/自建端点）
- 执行沙箱：`host`（hidden_tests / mutation_tests 在其中运行）
- LLM 评审（judge）：未开启（纯机器判分）
- 任务数：5

## 1. 模型横向对比

| 模型 | 通过率 | 加权得分 | 延迟 P50 | 延迟 P95 | tokens | 成本 |
|---|---|---|---|---|---|---|
| `mock-gpt` | 0/5（0%） | 0.200 | 30 ms | 38 ms | 450 | unmetered（网关未返回计费） |

## 2. 逐任务得分

| 任务 | 角色 | 模型 | 得分 | 结果 | 关键判据 |
|---|---|---|---|---|---|
| coder_slugify | coder | `mock-gpt` | 0.000 | ❌ | hidden_tests:输出中没有可提取的代码块 |
| coder_parse_duration | coder | `mock-gpt` | 0.000 | ❌ | hidden_tests:输出中没有可提取的代码块 |
| prd_export_feature | prd | `mock-gpt` | 0.333 | ❌ | required_sections:缺少小节：边界与非目标；不得出现占位表述:命中禁止表述：'TBD'（不得出现占位表述），共 2 处 |
| tech_design_inventory_api | tech_design | `mock-gpt` | 0.667 | ❌ | required_sections:缺少小节：接口定义, 数据模型, 风险与回滚 |
| tester_defect_report | tester | `mock-gpt` | 0.000 | ❌ | json_schema:输出不是合法 JSON（Expecting value: line 1 column 1 (char 0)） |

## 3. 失败明细（可直接复核证据）

### coder_slugify · `mock-gpt`（得分 0.000）
- ❌ **hidden_tests**（hidden_tests）：输出中没有可提取的代码块

### coder_parse_duration · `mock-gpt`（得分 0.000）
- ❌ **hidden_tests**（hidden_tests）：输出中没有可提取的代码块

### prd_export_feature · `mock-gpt`（得分 0.333）
- ❌ **required_sections**（required_sections）：缺少小节：边界与非目标
  ```
  # PRD 草案（mock-mock-gpt 生成）

## 需求
为「报表导出」功能写一份 PRD 草案（市场规模、竞品分析不需要）。必须包含小节：
`## 需求`、`## 验收标准`、`## 边界与非目标`。
验收标准必须用 `WHEN ... THEN ...` 的句式逐条书写，至少 3 条。
不要出现「待定」「TBD」「后续补充」这类占位表述。


## 验收标准
- WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据
- WHEN 用户点击导出 THEN 系统生成 CSV 文件并开始下载

  ```
- ❌ **不得出现占位表述**（forbidden_regex）：命中禁止表述：'TBD'（不得出现占位表述），共 2 处
  ```
  与非目标`。
验收标准必须用 `WHEN ... THEN ...` 的句式逐条书写，至少 3 条。
不要出现「待定」「TBD」「后续补充」这类占位表述。


## 验收标准
- WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据

  ```

### tech_design_inventory_api · `mock-gpt`（得分 0.667）
- ❌ **required_sections**（required_sections）：缺少小节：接口定义, 数据模型, 风险与回滚
  ```
  # PRD 草案（mock-mock-gpt 生成）

## 需求
为「库存扣减接口」写技术方案。必须包含小节：`## 接口定义`、`## 数据模型`、`## 风险与回滚`。
接口定义需给出 HTTP 方法与路径（例如 `POST /api/v1/inventory/deduct`）。
风险与回滚小节必须明确写出回滚方案。


## 验收标准
- WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据
- WHEN 用户点击导出 THEN 系统生成 CSV 文件并开始下载

  ```

### tester_defect_report · `mock-gpt`（得分 0.000）
- ❌ **json_schema**（json_schema）：输出不是合法 JSON（Expecting value: line 1 column 1 (char 0)）
  ```
  # PRD 草案（mock-mock-gpt 生成）

## 需求
下面是 CI 失败日志，请产出一份**结构化缺陷单**，只输出 JSON（不要 markdown 代码块、不要解释）：

```
=========================== short test summary info ============================
FAILED tests/test_report.py::test_export_empty_range -

## 验收标准
- WHEN 用户按日期筛选 THEN 系统仅返回所选日期范围的数据
- WHEN 用户点击导出 THEN 系统
  ```


## 4. 口径与限制（诚实声明）

- 判分为**机器判定**：代码类在沙箱跑隐藏测试（通过比例）、用例类用变异检验（能不能抓到 bug）、
  结构化产出校验 JSON Schema、文档/诊断校验必备小节与关键表述；均不依赖模型自评。
- 成本仅取网关 `x-litellm-response-cost` 头；**拿不到就标 unmetered，不做估算**
  （自定义 DashScope 端点通常无价格映射）。
- 延迟为端到端单次调用耗时（含网关开销），受网络与网关排队影响，仅供同轮横向比较。
- 金标集规模有限（当前 6 个任务），结论用于**排除明显不适用的模型**，不构成精细排名。
- 私有化模型接入：设置 `EVAL_BASE_URL` / `EVAL_API_KEY` / `EVAL_MODEL` 指向任意 OpenAI 兼容端点即可复用本报告。
