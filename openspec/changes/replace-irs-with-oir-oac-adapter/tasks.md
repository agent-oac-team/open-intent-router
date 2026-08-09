## 0. Superseded 历史记录

> 2026-07-23 以前在本 change 中完成的 OIR Knowledge Asset/Chunk/Admin、Knowledge
> Compat Facade、`oir_knowledge_vectors` 重建与相关 Shadow 验证均为 `superseded`。
> 它们只保留为历史实现事实，不再代表当前架构、能力归属或待办。可复用契约 fixture 与
> golden 按任务 2 迁往 `knowledge_sys`。

## 1. 唯一迁移计划与契约归属

- [x] 1.1 [#3] 重写本活动 OpenSpec，明确 OIR 中控事实源、`knowledge_sys` 知识事实源和 Provider-neutral 边界
- [x] 1.2 [#4] 将 Knowledge Contract Fixtures、270 Chunk Golden Dataset 和兼容测试迁入 `knowledge_sys`

## 2. OIR Memory 与 Knowledge Provider

- [x] 2.1 [#5] 扩展显式 `MEMORY_*` 配置并记录 Collection、数量、Recall 与 CRUD 不变性基线
- [x] 2.2 [#6] 在 OIR 引入独立于 Evidence Provider 的 Provider-neutral `KnowledgeProvider.retrieve`
- [x] 2.3 [#7] 实现 Knowledge Requirement、Mode 与受信 Knowledge Context Handle
- [x] 2.4 [#8] 让 Agent 仅瞬时消费 Knowledge 正文，长期记录只保存 Citation、Trace 和 Item 引用
- [x] 2.5 [#9] 删除 Memory 对 `KNOWLEDGE_*` 的回退、Transition 元数据和交叉校验，证明数据未重建或迁移

## 3. knowledge_sys 知识事实源

- [x] 3.1 [#10] 扩展 `knowledge_sys` 正式身份、健康与部署元数据，同时保留迁移期旧源码标识
- [x] 3.2 [#11] 建立 OAC/OIR Issuer 到 `knowledge_sys` 的短时 JWT/JWKS 信任边界与 read/admin Scope
- [x] 3.3 [#12] 实现 `trace_id + item_id` Retrieval Trace 审计、30 天可配保留和默认 10 秒内部预算

## 4. 下游切换与 OIR Knowledge 删除

- [x] 4.1 [#13] 将 OAC 浏览器与 Coze Knowledge 链路切到显式 `knowledge_sys` Base URL 和 JWT
- [x] 4.2 [#14] 实现 OIR 到 `knowledge_sys` 的 HTTP Provider Adapter、12 秒 Deadline、无重试和 Circuit
- [x] 4.3 [#15] 删除 OIR Knowledge HTTP、OAC Knowledge Host Adapter、Capability、Fallback 和 Write Fence
- [x] 4.4 [#16] 生成无正文 Manifest，清理 OIR Knowledge 表/向量/文件/持久化正文并证明 Memory 未变

## 5. IRS 中控退役

- [x] 5.1 [#17] 删除飞书 Agent Registry Sync、实现和相关配置
- [x] 5.2 [#18] 完成 OIR Central/Registry/Agent/Session/Event/Plan/Run/Result 承接、E2E、排空、Watermark、快照和回滚演练门禁
- [x] 5.3 [#19] 门禁通过后从 `knowledge_sys` 删除 Central、Registry、Session、Event、Plan、Result、Route Log 和 DeepSeek 中控模块及表

## 6. 正式命名与环境切换

- [x] 6.1 [#20] 将 `knowledge_sys` 仓库内部当前代码、测试、文档和部署自动化改用正式产品词汇
- [x] 6.2 [#21] 更新 OIR/OAC 跨仓配置、测试、文档和部署引用，Central 与 Knowledge URL 独立
- [ ] 6.3 [#22] 收缩旧仓库、distribution、包、进程和构建标识，不修改 SQL/Milvus Storage Identifier
  - [x] 6.3a OIR Host Runtime 删除 IRS fallback 客户端、网关、配置、DI、指标和演练入口；中控失败 fail closed
- [x] 6.4 [#23] 切换测试环境到 `knowledge-sys-test`，停止 `oac-central-test` 并运行全部门禁
- [ ] 6.5 [#24] 同轮发布生产 `knowledge-sys` 并永久退役 IRS 中控，不以 IRS 为回滚目标
- [ ] 6.6 [#25] 旧 Knowledge 入口连续 7 天零流量后删除兼容和凭据转换，完成 OpenSpec、文档和证据验收
