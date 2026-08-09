## 1. OpenSpec 与存储契约

- [x] 1.1 建立通用 Execution Trace schema、12 个事件家族和 `facts` 白名单校验
- [x] 1.2 建立单表 `execution_trace_events`、事件 offset、来源幂等约束和查询索引
- [x] 1.3 实现 Memory/Database Trace Event Writer 与 source-idempotent 冲突语义
- [x] 1.4 实现 owned Snapshot、Trace Completeness 和 Recovered Snapshot 标记
- [x] 1.5 实现 Snapshot 水位后的 SSE、`Last-Event-ID` 续传和所有权复核

## 2. OIR 生命周期投影

- [x] 2.1 通过应用端口投影 Canonical Turn、Route Decision、Agent Run/Event/Result 和完整性缺口
- [x] 2.2 接入真实 Delegated Run 进度与终态，不改变 Ticket、Run 或 Turn 权威状态机
- [x] 2.3 接入真实 UI Handoff 的 requested/completed/failed 事实
- [x] 2.4 接入已有 Context/Recall、Memory Formation/Decision/Revision 的有限事实投影

## 3. OAC Host 与代理

- [x] 3.1 增加 OAC Host Adapter 的 V2 受信 Snapshot/SSE 入口，仅调用应用端口
- [x] 3.2 增加 OAC Go Session 所有权验证与 Trace JSON/SSE 代理
- [x] 3.3 保证删除或越权 OAC Session 不可查询 Trace，且不跨库删除 OIR Canonical Data

## 4. OAC 工作台

- [x] 4.1 增加本人 Session、真实会话和当前轮次 Trace 三栏工作台
- [x] 4.2 以确定性业务文案显示业务运行、业务证据和脱敏技术证据
- [x] 4.3 接入 Snapshot + SSE，展示不完整和恢复状态，不猜测 Provider 进度
- [x] 4.4 由全局路由确认 UI Handoff completed/failed，并支持返回后恢复选择

## 5. 验证

- [x] 5.1 增加 OIR Writer/数据库/API/SSE 外部契约与失败降级测试
- [x] 5.2 增加 OAC Go 所有权代理与签名转发测试，以及 Client 组件行为测试
- [x] 5.3 覆盖 Coze、Delegated Run、页面交接、Context/Recall、Memory Formation 五个真实事实链路的本地集成测试
- [x] 5.4 完成本地 `3000 -> 8182 -> 8280` 浏览器 E2E；不执行部署、测试环境操作或归档
- [x] 5.5 运行 pytest、ruff、Go tests、Client tests/build、OpenSpec strict validate 和代码审查

## 本地验证记录（2026-07-22）

- OIR：`.venv/bin/python -m pytest` 通过（`985 passed, 1 skipped`）；`.venv/bin/python -m ruff check .` 与 `.venv/bin/python -m ruff format --check .` 通过。
- OAC：`apps/server` 中 `go test ./...` 通过；`apps/client` 中 `corepack pnpm exec tsx --test src/lib/coze-api.test.ts` 通过（2 tests），`corepack pnpm --filter @oac/client build` 通过。
- 变更经 OIR Spec 与标准审查；`openspec validate add-runtime-observation-execution-trace --strict` 通过。
- 已启动本机 OIR Host `8280`、OAC Go `8182` 和 Client `3000`，三者健康检查通过，工作台三栏页面已在浏览器渲染。现有浏览器令牌被本机 Go 拒绝为无效或过期，未能进入真实 Session、Route 或 Trace；未创建本地测试账号，因此 `5.4` 保持未完成。
- 上述均为本地证据。未部署、未访问测试环境，也未归档本变更。

## 本地浏览器 E2E 补充记录（2026-07-23）

- 在隔离 PostgreSQL `oac_e2e` 中使用本地测试账号，从 `http://lvh.me:3000/dashboard` 的真实 AI 侧栏提交不含真实客户或个人数据的“星河咖啡”合成需求。
- OIR Registry 真实返回 9 个已启用 OAC Agent；中控 Route Decision 选择 `strategy_analysis`，OAC Client 调用 Workflow `7658193531173126184`。本地 Provider 代理返回 500 后，Client 产生受控、脱敏、可重试失败，并将真实失败回调写入同一 Canonical Turn。
- OAC Session `1` 与 Canonical Turn `turn_ec0a9ac168ba4cad9e334829197cc887` 稳定关联；工作台显示同一真实对话，以及请求接收、处理判断、上下文准备、执行启动、执行失败和失败结果。
- Snapshot 与 SSE 均经 `3000 -> 8182 -> 8280` 返回 200；浏览器刷新后恢复同一 Session/Turn，Trace Completeness 显示“完整”。
- E2E 过程中补齐 Client runtime-observation rewrite、当前 OAC Bundle 请求头，以及空 Session 列表必须序列化为 `[]` 的 API 回归覆盖。
- 两轴复审发现并关闭首发范围内两项偏差：SSE 建连后的 Trace 缺口现在会增量发送新的 `execution_trace_meta`；Business Evidence 与 Technical Evidence 均按需展开。相关端点回归测试与 Client build 通过。
- 受治理 Memory 冲突的确认/拒绝操作仍归属后续 issue 12/13 与未完成的 R2/R3 门禁，本薄切片只展示真实 Memory 事实，不提前扩展写操作。
- 全程未部署、未访问测试环境、未推送、未归档，也未将 G0/R1/R2/R3 标记完成。
