# 模型评估报告（离线金标集 + 沙箱判分）

- 评估时间：2026-09-16T01:37:46.717451+00:00 → 2026-09-16T01:43:27.331950+00:00
- 评估端点：`http://127.0.0.1:4000/v1`（`EVAL_BASE_URL` 可指向私有化/自建端点）
- 执行沙箱：`host`（hidden_tests / mutation_tests 在其中运行）
- LLM 评审（judge）：未开启（纯机器判分）
- 任务数：10

## 1. 模型横向对比

| 模型 | 通过率 | 加权得分 | 延迟 P50 | 延迟 P95 | tokens | 成本 |
|---|---|---|---|---|---|---|
| `deepseek-chat` | 9/10（90%） | 0.935 | 1864 ms | 7645 ms | 7482 | $0.007633 |
| `qwen-plus` | 10/10（100%） | 1.000 | 8056 ms | 40267 ms | 7828 | $0.008068 |
| `qwen-max` | 8/10（80%） | 0.952 | 14625 ms | 25845 ms | 5796 | $0.029141 |

## 2. 逐任务得分

| 任务 | 角色 | 模型 | 得分 | 结果 | 关键判据 |
|---|---|---|---|---|---|
| coder_slugify | coder | `deepseek-chat` | 1.000 | ✅ | hidden_tests:隐藏测试 5/5 通过（returncode=0） |
| coder_parse_duration | coder | `deepseek-chat` | 1.000 | ✅ | hidden_tests:隐藏测试 6/6 通过（returncode=0） |
| prd_export_feature | prd | `deepseek-chat` | 1.000 | ✅ | required_sections:必备小节齐备；至少 3 条 WHEN...THEN 验收标准:匹配 7 处（要求 ≥3）：至少 3 条 WHEN...THEN 验收标准 |
| tech_design_inventory_api | tech_design | `deepseek-chat` | 1.000 | ✅ | required_sections:必备小节齐备；给出 HTTP 方法与路径:匹配 2 处（要求 ≥1）：给出 HTTP 方法与路径 |
| tester_defect_report | tester | `deepseek-chat` | 1.000 | ✅ | json_schema:JSON Schema 校验通过 |
| tester_test_generation_kills_mutant | tester | `deepseek-chat` | 1.000 | ✅ | mutation_tests:用例在正确实现上全绿（8 passed），并成功捕获含 bug 实现 ✅ |
| ops_pool_exhaustion | ops_diagnose | `deepseek-chat` | 1.000 | ✅ | 定位到连接池:命中关键词 ['连接池']（要求 all：['连接池']）；引用超时/连接池证据:命中关键词 ['超时', 'pool']（要求 any：['超时', 'pool']） |
| coder_moving_average | coder | `deepseek-chat` | 1.000 | ✅ | hidden_tests:隐藏测试 7/7 通过（returncode=0） |
| ops_misleading_signal | ops_diagnose | `deepseek-chat` | 1.000 | ✅ | required_sections:必备小节齐备；定位到线程池/队列:命中关键词 ['线程池', '队列']（要求 any：['线程池', '队列']） |
| prd_contradiction_scan | prd | `deepseek-chat` | 0.500 | ❌ | json_schema:Schema 不满足：$.severity: 缺少必填字段 |
| coder_slugify | coder | `qwen-plus` | 1.000 | ✅ | hidden_tests:隐藏测试 5/5 通过（returncode=0） |
| coder_parse_duration | coder | `qwen-plus` | 1.000 | ✅ | hidden_tests:隐藏测试 6/6 通过（returncode=0） |
| prd_export_feature | prd | `qwen-plus` | 1.000 | ✅ | required_sections:必备小节齐备；至少 3 条 WHEN...THEN 验收标准:匹配 3 处（要求 ≥3）：至少 3 条 WHEN...THEN 验收标准 |
| tech_design_inventory_api | tech_design | `qwen-plus` | 1.000 | ✅ | required_sections:必备小节齐备；给出 HTTP 方法与路径:匹配 1 处（要求 ≥1）：给出 HTTP 方法与路径 |
| tester_defect_report | tester | `qwen-plus` | 1.000 | ✅ | json_schema:JSON Schema 校验通过 |
| tester_test_generation_kills_mutant | tester | `qwen-plus` | 1.000 | ✅ | mutation_tests:用例在正确实现上全绿（2 passed），并成功捕获含 bug 实现 ✅ |
| ops_pool_exhaustion | ops_diagnose | `qwen-plus` | 1.000 | ✅ | 定位到连接池:命中关键词 ['连接池']（要求 all：['连接池']）；引用超时/连接池证据:命中关键词 ['超时', 'pool']（要求 any：['超时', 'pool']） |
| coder_moving_average | coder | `qwen-plus` | 1.000 | ✅ | hidden_tests:隐藏测试 7/7 通过（returncode=0） |
| ops_misleading_signal | ops_diagnose | `qwen-plus` | 1.000 | ✅ | required_sections:必备小节齐备；定位到线程池/队列:命中关键词 ['线程池', '队列']（要求 any：['线程池', '队列']） |
| prd_contradiction_scan | prd | `qwen-plus` | 1.000 | ✅ | json_schema:JSON Schema 校验通过；矛盾说明需引用具体约束:匹配 13 处（要求 ≥2）：矛盾说明需引用具体约束 |
| coder_slugify | coder | `qwen-max` | 1.000 | ✅ | hidden_tests:隐藏测试 5/5 通过（returncode=0） |
| coder_parse_duration | coder | `qwen-max` | 0.667 | ❌ | hidden_tests:隐藏测试 4/6 通过（returncode=1） |
| prd_export_feature | prd | `qwen-max` | 1.000 | ✅ | required_sections:必备小节齐备；至少 3 条 WHEN...THEN 验收标准:匹配 3 处（要求 ≥3）：至少 3 条 WHEN...THEN 验收标准 |
| tech_design_inventory_api | tech_design | `qwen-max` | 1.000 | ✅ | required_sections:必备小节齐备；给出 HTTP 方法与路径:匹配 1 处（要求 ≥1）：给出 HTTP 方法与路径 |
| tester_defect_report | tester | `qwen-max` | 1.000 | ✅ | json_schema:JSON Schema 校验通过 |
| tester_test_generation_kills_mutant | tester | `qwen-max` | 1.000 | ✅ | mutation_tests:用例在正确实现上全绿（3 passed），并成功捕获含 bug 实现 ✅ |
| ops_pool_exhaustion | ops_diagnose | `qwen-max` | 1.000 | ✅ | 定位到连接池:命中关键词 ['连接池']（要求 all：['连接池']）；引用超时/连接池证据:命中关键词 ['超时']（要求 any：['超时', 'pool']） |
| coder_moving_average | coder | `qwen-max` | 0.857 | ❌ | hidden_tests:隐藏测试 6/7 通过（returncode=1） |
| ops_misleading_signal | ops_diagnose | `qwen-max` | 1.000 | ✅ | required_sections:必备小节齐备；定位到线程池/队列:命中关键词 ['线程池', '队列']（要求 any：['线程池', '队列']） |
| prd_contradiction_scan | prd | `qwen-max` | 1.000 | ✅ | json_schema:JSON Schema 校验通过；矛盾说明需引用具体约束:匹配 8 处（要求 ≥2）：矛盾说明需引用具体约束 |

## 3. 失败明细（可直接复核证据）

### prd_contradiction_scan · `deepseek-chat`（得分 0.500）
- ❌ **json_schema**（json_schema）：Schema 不满足：$.severity: 缺少必填字段
  ```
  {"contradictions": [{"ids": [1, 2, 3, 4], "reason": "同步接口不引入队列与异步任务，且导出文件包含全部字段且不压缩，单次最多500万行，在用户点击后1秒内返回完整文件在常规网络、存储和序列化条件下不可同时满足。"}, {"ids": [1, 5], "reason": "要求用户点击后1秒内返回完整文件，同时接口P99延迟必须保持在200ms以内，二者对响应时间上限的要求不一致且后者更严格，无法同时作为同一接口的硬性指标。"}, {"ids": [2, 3, 4, 5], "reason": "同步接口、不压缩、全字段导出最多500万行数据，通常需要较长处理与传输时间，无法保证P99延迟在200ms以内。"}]}
  ```

### coder_parse_duration · `qwen-max`（得分 0.667）
- ❌ **hidden_tests**（hidden_tests）：隐藏测试 4/6 通过（returncode=1）
  ```
   literal for int() with base 10: ''

solution.py:23: ValueError
_______________________________ test_hours_only ________________________________

    def test_hours_only():
>       assert parse_duration("2h") == 7200
E       AssertionError: assert 0 == 7200
E        +  where 0 = parse_duration('2h')

test_hidden.py:11: AssertionError
=========================== short test summary info ============================
FAILED test_hidden.py::test_hours_minutes - ValueError: invalid literal for i...
FA
  ```

### coder_moving_average · `qwen-max`（得分 0.857）
- ❌ **hidden_tests**（hidden_tests）：隐藏测试 6/7 通过（returncode=1）
  ```
  ================================
________________________ test_window_larger_than_input _________________________

    def test_window_larger_than_input():
>       assert moving_average([1, 2], 5) == [None, None]
E       assert [None, None, None, None] == [None, None]
E         
E         Left contains 2 more items, first extra item: None
E         Use -v to get more diff

test_hidden.py:14: AssertionError
=========================== short test summary info ============================
FAILED te
  ```


## 4. 口径与限制（诚实声明）

- 判分为**机器判定**：代码类在沙箱跑隐藏测试（通过比例）、用例类用变异检验（能不能抓到 bug）、
  结构化产出校验 JSON Schema、文档/诊断校验必备小节与关键表述；均不依赖模型自评。
- 成本仅取网关 `x-litellm-response-cost` 头；**拿不到就标 unmetered，不做估算**
  （自定义 DashScope 端点通常无价格映射）。
- 延迟为端到端单次调用耗时（含网关开销），受网络与网关排队影响，仅供同轮横向比较。
- 金标集规模有限（当前 6 个任务），结论用于**排除明显不适用的模型**，不构成精细排名。
- 私有化模型接入：设置 `EVAL_BASE_URL` / `EVAL_API_KEY` / `EVAL_MODEL` 指向任意 OpenAI 兼容端点即可复用本报告。
