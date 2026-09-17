# 约束：命名与目录

> 课程图 20/21：局部正确不等于系统一致——表名 `sys_user` 与 `t_users`、状态 `tinyint` 与 `boolean`，
> 两段代码各自可运行却无法组合。**命名与结构必须遵守同一依据。**

## 命名

- 数据表：`snake_case` 复数，禁止 `t_` / `sys_` 前缀混用；沿用既有表时以既有命名为准；
- 字段：`snake_case`；布尔字段以 `is_` / `has_` 开头；
- 权限点：`<资源>:<动作>`，如 `export:user`、`report:read`；
- 事件：`<域>.<动作>`，如 `approval.decided`、`task.created`；
- Python 模块/函数：`snake_case`；类：`PascalCase`。

## 目录

```
platform/services/<service>/app/     服务代码（每个服务自带 tests/ 与 pyproject.toml）
platform/contracts/                  跨服务契约（事件、Schema）
platform/engineering-gates/          工程闸门规则（规则即代码）
platform/project-rules/              本规则包（约束/设计/完整性）
platform/scripts/                    演示、演练与运维脚本
platform/reports/                    演练与验收报告（留证）
```

## 禁止事项

- 禁止在业务代码里硬编码供应商 URL/密钥（走环境变量）；
- 禁止新增服务前不更新架构文档（16 号文档 §2 的"何时才该拆服务"判据）；
- 禁止改动既有接口的返回结构而不升版本。
