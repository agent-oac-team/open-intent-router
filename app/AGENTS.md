# OIR Core 作用域规则

本文件适用于 `app/**`，补充根 `AGENTS.md`。

## Core 边界

- Core 必须保持宿主无关。公共 Schema、Service、Prompt、模型和数据库表中不得引入 OAC、IRS、Coze、飞书、`bot_id`、`route_path`、运营版或展业版语义。
- 宿主能力通过 `app/application/ports.py` 暴露。不要为了 Adapter 方便而让 Host 直接操作 Core repository 私有状态。
- `app/api` 只做协议适配、依赖注入和错误转换；业务不变量放在 Service / transaction store。
- 新执行路径复用现有 Router、Invocation、Plan、Turn、Memory 和 Knowledge 生命周期，不建立第二套状态机。

## 事实与安全

- PostgreSQL canonical 状态优先于 Session 展示消息、Route Log、Debug Trace、mem0 和 Milvus。
- 权限、所有权、TTL、删除状态和 source policy 必须在 LLM、Invoker、Provider 或响应正文之前校验。
- 外部调用不得持有数据库事务。涉及 Turn / Run / Result / Plan / Outbox 时，保持现有事务、幂等和重试不变量。
- 配置定义只进入 `app/core/config.py`，示例同步到 `.env.example`，不得记录真实凭证。

## 变更前后

- 修改前读取 [应用地图](../docs/App-Desc/README.md) 和相关 Schema、Service、Repository、测试及 OpenSpec。
- API 变更同步 `docs/App-Desc/contracts/api.md`；数据 / 事务变更同步对应架构文档；运行参数变更同步 Runbook。
- 至少运行受影响 pytest、Ruff lint 和 format check；具体矩阵见 [App-Adr](../docs/App-Adr/README.md)。
