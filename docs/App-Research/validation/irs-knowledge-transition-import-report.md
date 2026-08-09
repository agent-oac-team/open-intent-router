# IRS 测试环境知识补数验收记录

> 状态：2026-07-21 测试环境验收记录。2026-07-30 已暂停在 OIR 内接通
> canonical Knowledge 语义检索；本文只保留已经发生的 IRS 补数与旧链路验证，
> 不作为 OIR 后续知识库研发或切流任务来源。

## 验收范围

在知识能力长期归属重新决策前，为仍调用旧 `/central-api` 的下游提供过渡保障，
将 `/data/内容生产` 的 6 份工作簿增量写入 IRS 测试环境：

- PostgreSQL：`oac_test.knowledge_assets`、`oac_test.knowledge_chunks`
- Milvus：`oac_knowledge_chunks`
- 稳定资产 ID：`asset_content_production_01` 至
  `asset_content_production_06`

本次操作未使用 `--clear-all`，只替换同名稳定资产 ID 的记录和向量。源文件上传前后
SHA-256 已逐文件比对一致。

## 数据对账

| asset key | asset ID | PostgreSQL chunk | Milvus vector |
| --- | --- | ---: | ---: |
| 01 | `asset_content_production_01` | 45 | 45 |
| 02 | `asset_content_production_02` | 171 | 171 |
| 03 | `asset_content_production_03` | 0 | 0 |
| 04 | `asset_content_production_04` | 23 | 23 |
| 05 | `asset_content_production_05` | 25 | 25 |
| 06 | `asset_content_production_06` | 6 | 6 |

IRS 旧解析器对 `01 要素表` 生成 45 个 chunk，因此总数为 270；这与当时 OIR
canonical 导入的 269 条存在 1 条解析差异。270 个非空 chunk 均已写入
`index_refs`，Milvus 中按稳定 asset ID 查询的数量与 PostgreSQL 一致。

原有随机 ID 资产
`asset_file_71d2fe2f8348490abb8ccac1ae08dfe9`（风险违规词汇总表）和
`asset_file_87779774f53c449bb5b76cd3f6984d5a`（6月活动）仍存在且启用，证明
本次操作没有清空已有资产。

## 旧链路验证

通过测试服务器本机
`127.0.0.1:8084/api/v1/knowledge/grouped-search`，使用原始长句和 6 个
asset key 验证：

- request ID：`req_codex_oac_import_verify_20260721`
- 整体 `matched=true`，01、02、04、05、06 各返回 3 条 evidence，03 保持空结果
- `04 活动表` 返回“资产配置有惊喜”和“升金大赢家MAX”相关证据
- trace ID：`k_trace_4d91a7afc3844cdbb4bfaad06e2047de`
- confidence：`0.6748`
- 六个资产均生成 `knowledge_search_traces`，接口无 provider warning

通过测试服务器内部调用公网 `/central-api` 网关，request ID
`req_codex_oac_gateway_verify_20260721` 同样得到 `matched=true` 和
`3 / 3 / 0 / 3 / 3 / 3` 条 evidence，证明当时 Nginx 到 IRS 的实际下游入口可用。

以上结果只证明 2026-07-21 IRS 测试环境旧知识链路的补数与可用性，不自动代表
当前环境，也不定义 OIR 的长期知识能力归属。
