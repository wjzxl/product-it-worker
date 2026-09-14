# 产品线（租户）SOP 覆盖配置

本目录存放**产品线级**的 SOP 参数覆盖，文件名即租户标识（对应接口头 `X-Tenant`）。

## 优先级

```
环境变量  >  本目录 <tenant>.yaml  >  ../sop-config.yaml（平台基线）  >  代码内置默认
```

只需写差异项；删除本产品线的文件即回退平台基线，不需改代码、不需重启（mtime 热更新）。

## 生成方式

```bash
cd platform
services/approval-gateway/.venv/bin/python scripts/onboard_product_line.py \
    --tenant line-pay --product-name "支付中台" --profile strict --verify
```

- `--profile strict|standard|fast`：按 AI 可自主程度选画像（严格/常规/快速）；
- `--verify`：接入后自动跑隔离验收演练（新线零信任起步、惩罚不跨线扩散）；
- `--offline`：只生成配置与报告，不调用在线服务。

## 隔离语义（重要）

- **信任按「产品线 × Gate」隔离**：`line-pay::GATE_PR_REVIEW` 与 `line-crm::GATE_PR_REVIEW` 各自独立，
  新产品线从零积累样本，**不会继承**其它产品线已达成的 L1 全自动；
- **惩罚不跨线**：某产品线一次退回/否决只把自己的该 Gate 打成 L3；
- **知识库按来源前缀隔离**：本产品线的 Runbook 放在 `<tenant>/runbooks/`，
  检索时用 `source_prefix` 限定，避免 Agent 引用别家流程。

## 已接入产品线

| 租户 | 画像 | 来源 |
|---|---|---|
| `line-pay` | strict | 接入脚本示例（含隔离验收演练记录：`../reports/onboard-line-pay-*.md`） |
