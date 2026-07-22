# OIR 语义检索接入与 IRS 过渡保障结论

> 状态：2026-07-21 当前问题结论与待实施设计。当前行为以代码、测试环境配置和数据库实测为准；完成实现后应将长期架构结论提升到 App-Desc，并以对应 OpenSpec 和测试作为验收依据。

## 1. 问题范围

OAC / Coze 的 IRS 兼容知识接口已经切换到 OIR Host Adapter，但迁移后的 canonical Knowledge 查询链路尚未真正接入 `oir_knowledge_vectors`。与此同时，仍调用旧 `/central-api` 的下游会进入 IRS，并查询 `oac_test` 与 `oac_knowledge_chunks`。

本结论覆盖两个目标：

1. 让 OIR 的 Search / Grouped Search 使用真实 embedding + Milvus 语义检索，并继续由 PostgreSQL canonical data 回填正文和执行权限治理。
2. 在最终切流完成前，将 6 份内容生产工作簿同步到 IRS 测试库，保证旧链路仍可提供知识检索能力。

## 2. 已确认事实

### 2.1 OIR canonical 数据和向量均已存在

- `oir_test.knowledge_assets` 已登记 `asset_content_production_01` 至 `asset_content_production_06`。
- 01、02、04、05、06 共包含 269 个 active canonical chunk；03 客群表为空，状态为 `deferred`。
- 269 个 chunk 已从 OIR canonical data 重建到 `oir_knowledge_vectors`，没有复制 IRS 旧向量。
- `04 活动表` 中存在“财富大赢家”和“升金大赢家MAX”等有效知识。

### 2.2 OAC / Coze 兼容查询当前没有使用向量

兼容 Handler 调用 `KnowledgeAssetService.search()` / `grouped_search()`。该服务从 `knowledge_asset_chunks` 读取全部候选，按中文二元词重合率评分，并丢弃分数低于 `0.2` 的 chunk。

因此，长句“客户最近工资到账了，想引导他把闲置资金做个短期配置，结合大赢家活动，帮我写一段企微1v1文案。”在 OIR 正确入口仍返回 `matched=false`；缩短为“大赢家活动”后，`04 活动表` 可以返回 3 条证据。这证明资产、权限和内容可读，失败点是检索算法而非数据缺失。

### 2.3 OIR 内已有另一套向量能力，但不能直接替换

`KnowledgeService` 与 `MilvusKnowledgeVectorStore` 已支持 embedding 和 Milvus search，但它们使用旧的 `knowledge_sources` / `knowledge_chunks` repository。迁移后的兼容接口使用 `knowledge_assets` / `knowledge_asset_chunks`。

直接把兼容 Handler 改为调用 `KnowledgeService`，或只设置 `KNOWLEDGE_VECTOR_BACKEND=milvus`，会产生以下问题：

- 请求 scope 使用 asset/group/stable key，而旧服务使用 source ID。
- Milvus 命中后旧 Store 会从 `knowledge_chunks` 回填，找不到 `knowledge_asset_chunks` 中的 canonical chunk。
- Grouped Search 的每资产 top-k、空资产槽位、兼容 warning 和 trace 语义无法直接保留。

## 3. 根因

迁移实现完成了 canonical Asset/Chunk、Grouped Search、权限治理、兼容响应投影和向量重建，但没有把 canonical Knowledge 应用服务与 Milvus 查询能力接通。迁移验收样本中的短查询可以通过词法评分，因此没有覆盖真实下游使用的长自然语言请求。

这属于运行链路缺口，不是缺少 embedding、Milvus collection 或迁移数据。

## 4. OIR 目标改造

### 4.1 新增 canonical 向量查询端口

向量层只负责返回受 scope 限制的命中标识和分数，不负责读取旧 Knowledge repository：

```python
class CanonicalKnowledgeVectorSearch(Protocol):
    async def embed_query(self, query: str) -> list[float]: ...

    async def search_by_vector(
        self,
        *,
        vector: list[float],
        asset_ids: list[str],
        limit_per_asset: int,
    ) -> list[CanonicalVectorHit]: ...
```

`CanonicalVectorHit` 至少包含 `chunk_id`、`asset_id` 和 `score`。Milvus 中现有 `source_id` 等于 canonical `asset_id`，可继续作为过滤字段；后续 schema 演进时再显式增加 tenant 字段。

### 4.2 改造 KnowledgeAssetService

Search / Grouped Search 应按以下顺序执行：

1. 从 PostgreSQL 解析 tenant、asset group、stable key 和 asset ID。
2. 在向量查询前过滤 disabled、deleted、deferred、secret 和无权限资产。
3. 每个请求只生成一次 query embedding。
4. 使用可见 asset ID 查询 `oir_knowledge_vectors`；Grouped Search 保留每资产独立 `top_k_per_asset`。
5. 使用 canonical repository 按 Milvus 命中的 `chunk_id` 回填 `knowledge_asset_chunks`。
6. 对回填结果再次校验 tenant、asset scope、chunk status 和 sensitivity，忽略陈旧或越界向量。
7. 保持现有 IRS 兼容 evidence、source_ref、confidence、warning 和 trace 响应结构。

### 4.3 失败与降级策略

- embedding 或 Milvus 超时、配置错误、维度不一致时返回明确 provider warning。
- 不允许无标识地退回词法检索，否则调用方无法判断证据质量变化。
- 如业务要求保留词法 fallback，必须返回 `semantic_fallback` warning，并在 trace 中记录 backend、collection、provider error 和 fallback outcome。
- Exact Read 继续只使用 PostgreSQL canonical data，不依赖 Milvus。

### 4.4 配置

完成代码接线后，测试环境应启用：

```env
KNOWLEDGE_VECTOR_BACKEND=milvus
KNOWLEDGE_MILVUS_COLLECTION=oir_knowledge_vectors
KNOWLEDGE_EMBEDDING_PROVIDER=openai_compatible
KNOWLEDGE_EMBEDDING_MODEL=text-embedding-v4
KNOWLEDGE_EMBEDDING_DIM=1024
```

URI、token、base URL 和 API key 只由部署 Secret 注入，不进入文档、日志或测试快照。

## 5. 测试要求

至少补充以下回归用例：

1. 当前真实长句必须在 `04 活动表` 命中“大赢家”相关证据。
2. 六资产 Grouped Search 只生成一次 query embedding，并按资产独立执行 top-k。
3. `03` 保留 deferred 空槽位，不误报为权限失败。
4. secret、disabled、deleted、跨 tenant、越权 asset 和陈旧向量不得返回。
5. Milvus 返回其他 asset 的 chunk 时必须被 canonical 二次校验丢弃。
6. embedding 超时、Milvus 失败和维度不一致必须产生稳定 warning。
7. Search / Grouped Search / Exact Read 的 IRS 兼容 JSON 结构保持不变。
8. `oir_knowledge_vectors` 记录数、canonical active chunk 数和 manifest 必须一致。

## 6. IRS 过渡保障

最终切流完成前，旧 `/central-api` 仍可能被下游调用。测试环境需要将 `/data/内容生产` 的 6 份工作簿同步写入：

- PostgreSQL：`oac_test.knowledge_assets`、`oac_test.knowledge_chunks`
- Milvus：`oac_knowledge_chunks`
- 稳定资产 ID：`asset_content_production_01` 至 `asset_content_production_06`

临时导入必须满足：

- 禁止使用 `--clear-all`，不得删除已有“风险违规词汇总表”和“6月活动”等资产。
- 只替换同名稳定资产 ID 的旧记录和对应向量。
- 使用测试环境现有 embedding 模型与 1024 维 collection。
- 导入后验证资产数、各资产 chunk 数、向量引用以及旧 Grouped Search。
- 过渡期间 IRS 与 OIR 不做双写主源设计；本次同步是明确的一次性兼容保障，OIR 仍是迁移目标事实源。

## 7. 切流门禁

只有同时满足以下条件，才能停止 IRS 临时数据保障：

1. Coze 和其他下游不再调用 `/central-api/api/v1/knowledge/*`。
2. OAC Go Coze 代理到 OIR 的签名、鉴权和超时路径通过测试环境验收。
3. OIR canonical 语义检索通过长句、权限和 Provider 故障用例。
4. 观测窗口内 IRS 无知识查询流量，且没有未登记调用方。
5. 回滚策略明确：仅只读知识允许在未提交条件下回退，不恢复 IRS 为知识主写源。

## 8. 2026-07-21 执行记录

已使用 IRS 测试环境现有配置执行一次增量导入，未使用 `--clear-all`。源文件为 `/data/内容生产` 下的 6 份工作簿，远端导入目录为 `/var/www/oac-central-test/.data/content-production-import-20260721`；上传前后 SHA-256 已逐文件比对一致。

独立查询确认 PostgreSQL 当前数据库为 `oac_test`，导入结果如下：

| asset key | asset ID | PostgreSQL chunk | Milvus vector |
| --- | --- | ---: | ---: |
| 01 | `asset_content_production_01` | 45 | 45 |
| 02 | `asset_content_production_02` | 171 | 171 |
| 03 | `asset_content_production_03` | 0 | 0 |
| 04 | `asset_content_production_04` | 23 | 23 |
| 05 | `asset_content_production_05` | 25 | 25 |
| 06 | `asset_content_production_06` | 6 | 6 |

IRS 旧解析器对 `01 要素表` 生成 45 个 chunk，因此本次 IRS 导入总数为 270；这与 OIR canonical 导入的 269 条存在 1 条解析差异，不应强行按 OIR 数量判断 IRS 导入失败。270 个非空 chunk 的 `index_refs` 均已写入，Milvus collection `oac_knowledge_chunks` 中按稳定 asset ID 查询的数量与 PostgreSQL 完全一致。

原有随机 ID 资产 `asset_file_71d2fe2f8348490abb8ccac1ae08dfe9`（风险违规词汇总表）和 `asset_file_87779774f53c449bb5b76cd3f6984d5a`（6月活动）仍存在且启用，证明本次操作没有清空已有资产。

通过测试服务器本机 `127.0.0.1:8084/api/v1/knowledge/grouped-search` 使用原始长句和 6 个 asset key 验证：

- 验证 request ID：`req_codex_oac_import_verify_20260721`
- 整体 `matched=true`，01、02、04、05、06 各返回 3 条 evidence，03 因源表无数据保持空结果。
- `04 活动表` 返回“资产配置有惊喜”和“升金大赢家MAX”相关证据，trace ID 为 `k_trace_4d91a7afc3844cdbb4bfaad06e2047de`，confidence 为 `0.6748`。
- 六个资产均生成 `knowledge_search_traces` 记录，接口无 provider warning。
- 通过测试服务器内部再次调用公网 `/central-api` 网关，request ID `req_codex_oac_gateway_verify_20260721` 同样得到 `matched=true` 和 `3 / 3 / 0 / 3 / 3 / 3` 条 evidence，证明 Nginx 到旧 IRS 的实际下游入口可用。

以上结果只完成 IRS 旧链路的临时补数和可用性保障。OIR canonical 查询接入 `oir_knowledge_vectors` 的代码改造仍须按第 4、5 节实施和验收。
