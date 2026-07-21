# OAC Host Composition 作用域规则

本文件适用于 `host_apps/oac/**`，补充根 `AGENTS.md`。

- 本目录只负责组装 OIR Core、OAC Adapter、Host 配置、中间件和启动门禁；不要在 composition root 中实现协议映射或领域状态机。
- Native API 固定挂载到独立 `/oir` 前缀，Legacy API 由 Adapter 拥有。
- 启动前校验 current-only V2 profile / key、唯一 Registry 写源、独立 database / collection、Write Fence 和 rehearsal 数据域。
- Host 配置定义集中在 `host_apps/oac/config.py`；`.env.example` 只保留无敏感值说明。
- 修改组合或配置后运行 Host 启动门禁、capability、boundary 和相关 transport tests，并同步 Host Runbook。
