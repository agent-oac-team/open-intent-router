# OAC Host Adapter 作用域规则

本文件适用于 `host_adapters/oac/**`，补充根 `AGENTS.md`。

## 防腐层边界

- 本目录可以表达 OAC、IRS、Coze 和 Legacy 契约，但这些概念不得进入 OIR Core 公共 Schema、Service、Prompt 或数据库模型。
- Adapter 只能调用 `app.application` 暴露的端口；不得直接修改 Core repository 私有状态，也不得复制 Canonical Turn、Delegated Run 或 Plan 状态机。
- Legacy 与 Native 同路径冲突必须由明确的 Host 入口 / 前缀解决，禁止按请求 Body 猜协议。

## 身份与治理

- 所有 Host 调用只接受 current-only `OIR-HOST-V2` 和对应 credential profile。不得恢复 V1、旧同步 Token、浏览器 Authorization 或 Body 自报字段作为下游授权证据。
- User、Admin、Coze key 与 credential class 隔离；签名绑定 method、path、query、Body hash、audience、timestamp、nonce 和 claims。
- Route 仅在 `not_accepted` 证明成立时可回退；Knowledge 只读按策略回退；所有 control / runtime write 和提交未知请求禁止 fallback、双写或盲目重试。
- Capability、日志、Trace 和报告只输出低基数脱敏信息，不暴露 subject、key ID、entitlement、签名、Ticket、Token、正文或连接串。

## 变更前后

- 修改前读取 [OAC Host Adapter](../../docs/App-Desc/architecture/oac-host-adapter.md)、[兼容矩阵](../../docs/App-Desc/contracts/legacy-native-compat-matrix.md)、[V2 Runbook](../../docs/App-Adr/develop/skills/runbooks/oac-host-signature-v2-runbook.md) 和相关 OpenSpec。
- 契约变化必须更新 compat fixture / replay，并覆盖正向调用及坏 key、profile、篡改、过期、重放、越权和提交未知。
- 不为让实现通过而改写冻结 IRS fixture；契约升级必须版本化并说明迁移策略。
