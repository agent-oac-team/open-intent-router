# IRS 知识接口契约差异记录

## 1. 基线范围

本记录对比以下三类证据：

1. 规范基线：`intent_recon_sys/docs/OAC共享知识库接口文档.md`。
2. 实现基线：IRS revision `348cf93fa3441108481631b7768d5ab7670c706e` 的 API、Schema 与 Service 代码，以及本地运行时 `/openapi.json`。
3. 行为基线：`openapi/`、`success/` 和 `errors/` 下的版本化脱敏 fixture。

本记录只处理《OAC 共享知识库接口文档》覆盖的 13 个 Knowledge 方法。Central 4 个方法和 Registry 5 个方法没有对应的共享知识库规范文档，其基线以运行时 OpenAPI、实现代码和 fixture 为准。

## 2. 总体结论

- 文档列出的 13 个 Knowledge 方法在 IRS 代码与运行时 OpenAPI 中全部存在，路径和 HTTP method 一致，没有能力缺失。
- 22 个迁移方法均已保存成功 fixture；知识错误/降级样本覆盖 `401`、`422`、无命中、权限过滤、Embedding 失败、向量检索失败、超时和截断。
- 文档与实际运行时的差异集中在 OpenAPI 声明、示例完整度和少数字段示例，不需要改变已确认的 Adapter/Core 架构。
- Adapter SHALL 保持实际 IRS 可解析形状，并保留文档定义的业务错误和 warning 语义。不得因 OpenAPI 未声明业务错误响应而删除 `400/401/404/502/503` 行为。

## 3. 逐接口对账

| 方法与路径 | 文档 | 代码/OpenAPI | 实际 fixture | Adapter 兼容结论 |
| --- | --- | --- | --- | --- |
| `POST /api/v1/admin/knowledge/files` | 同步返回 Asset + Job；管理 Token 必填 | 路径、multipart 字段和成功 Schema 一致；OpenAPI 只声明 `200/422` | `200` 返回 indexed Asset + Job | 保持同步 Asset + Job；补齐运行时业务错误状态投影 |
| `GET /api/v1/admin/knowledge/files` | 支持 7 类筛选，Token 必填 | 路径、查询参数和响应一致；筛选值为普通 string | `200` 返回 `items/total` | 保持全部筛选参数和当前宽松 string 输入 |
| `GET /api/v1/admin/knowledge/files/{asset_id}` | 返回 Asset、metadata、file metadata、latest job 和计数 | Schema 与字段一致 | `200` 字段齐全 | 原样兼容；不存在保持 `404` |
| `GET /api/v1/admin/knowledge/files/{asset_id}/chunks` | `offset=0`、`limit=50`、最大 200 | 参数约束和预览 Schema 一致 | `200` 返回 snippet，不含向量 | 原样兼容并禁止暴露 embedding/vector 正文 |
| `POST /api/v1/admin/knowledge/files/{asset_id}/retry` | 使用已存原文件和 canonical chunks 重建 | 行为一致；实际成功 Job 含非空 `file_metadata` | `200` 的 `file_metadata` 为已存原文件元数据 | 以实际运行时为准，允许并返回非空 `file_metadata` |
| `DELETE /api/v1/admin/knowledge/files/{asset_id}` | 软删除、清理向量并返回 cleanup warning | 行为和响应 Schema 一致 | `200` 返回 deleted Job | 原样兼容；清理失败仍先禁用 canonical 数据并返回 warning |
| `POST /api/v1/knowledge/search` | `scope` 优先、`filters` 兼容，Provider 故障用 HTTP 200 warning | 主 Schema 与行为一致；OpenAPI 还接受部分文档未列出的通用 return option | 成功、no-match、permission、Provider、timeout fixture 均为 `200` | 保持完整实际 Schema；不得把 Provider 故障降成无 warning 的普通未命中 |
| `POST /api/v1/knowledge/grouped-search` | 稳定资产组/键、每资产独立结果、可保留空槽位 | Schema、400/422 校验和治理逻辑一致 | `200` 返回稳定 `assets.04` 结构 | 原样兼容；`content_production` 与 `01` 至 `06` 为 Adapter 稳定外部契约 |
| `POST /api/v1/knowledge/read` | 支持五种 target、分页、预算与治理 | Schema 和 Service 行为一致 | 成功读取与 `result_truncated` fixture 均为 `200` | 原样兼容；missing/filtered/ambiguous 必须保持结构化 warning |
| `GET /api/v1/knowledge/assets` | 资产发现和权限过滤 | 路径一致；实际 query 参数比文档概览更完整 | `200` 返回可见资产列表 | 保持运行时完整 query surface 和逐目标治理 |
| `GET /api/v1/knowledge/assets/{asset_id}` | 读取单个可访问 Asset | 路径和 Schema 一致 | `200` 返回 Asset detail | 不存在或被过滤的语义按实际 warning/空 Asset 投影，不泄漏正文 |
| `GET /api/v1/knowledge/assets/{asset_id}/chunks` | 分页精确读取可访问 chunks | 实际 query 支持全部 return option | `200` 返回 canonical chunks | 保持实际完整参数面；只读 PostgreSQL canonical 数据 |
| `GET /api/v1/knowledge/chunks/{chunk_id}` | 读取单个可访问 Chunk | 实际 query 支持预算和可选大字段 | `200` 返回目标 Chunk | 保持实际完整参数面和逐目标权限过滤 |

## 4. 已证实差异与决策

| ID | 差异 | 证据 | 兼容决策 |
| --- | --- | --- | --- |
| KD-001 | OpenAPI 将 `X-Admin-Sync-Token` 表示为 optional，因为 Handler 参数允许 `None`；文档和运行时都要求有效 Token | OpenAPI operation 参数；`registry-unauthorized.json`、`knowledge-admin-unauthorized.json` | Adapter 运行时强制受信凭证；Legacy Schema 保持旧客户端可发送方式，缺失/错误继续返回 `401` |
| KD-002 | OpenAPI 只自动声明成功和 `422`，未声明代码实际返回的 `400/401/404/502/503` | `app/api/knowledge_admin.py`；错误响应章节 | Adapter OpenAPI 明确补齐这些业务错误响应，但保持原 HTTP 状态与 `detail` 语义 |
| KD-003 | 文档的 Provider 故障示例省略 `request_id`、`confidence` 和 `trace_id`，实际响应模型要求这些字段 | `KnowledgeSearchResponse`；`knowledge-vector-search-error.json` | 以实际完整响应模型为准，不按不完整示例删字段 |
| KD-004 | 文档 Retry 示例把 `job.file_metadata` 写为 `null`，实际成功 Retry 会复制已存原文件元数据 | `KnowledgeAdminService.retry`；`knowledge-admin-retry.json` | 返回实际非空 `file_metadata`；客户端仍需兼容 nullable Schema |
| KD-005 | Search 文档只列出常用 return options；实际共用 `KnowledgeReturnOptions`，还接受 `include_source_ref`、`offset` 和 `limit` | OpenAPI `KnowledgeReturnOptions` | Adapter 接受实际 IRS 的完整字段集合；对 Search 中无业务作用的字段不得产生越权或额外数据 |
| KD-006 | GET Read/Asset 接口的文档用“return_options”概括 query；实际将每个选项展开成独立 query 参数，并使用逗号分隔 `user_tags/asset_ids` | 四个 GET operation snapshot | Adapter 按实际展开参数兼容，不要求调用方改为 JSON object |
| KD-007 | 文档明确检索接口当前不强制 Token，实际授权依赖可伪造的 Body/query 身份字段 | 文档 3.1、13.5、18；运行时 OpenAPI | 迁移不延续该安全缺口：Adapter 保留旧 Body/query 形状，但最终 tenant/user/groups 必须来自已确认的 Identity Bridge |

## 5. Warning 与错误基线

| 场景 | HTTP | 必要结果 |
| --- | --- | --- |
| 请求 Schema 验证失败 | `422` | FastAPI/Pydantic `detail[]` 形状 |
| 管理凭证缺失或错误 | `401` | `detail="Invalid admin sync token"` 语义 |
| 正常无命中 | `200` | `matched=false`、空 evidence、`no_match` |
| 权限过滤 | `200` | 不返回无权正文，并包含 `permission_filtered` 或对应治理 warning |
| Embedding 失败 | `200` | `embedding_error` |
| 向量检索失败 | `200` | `vector_search_error` |
| 检索超时 | `200` | `timeout` |
| 精确读取超预算 | `200` | 截断正文并包含 `result_truncated`，必要时同时包含 cap warning |

## 6. 未决项

本轮没有发现需要改变已确认架构、安全边界或外部接口范围的新事实。性能、熔断与稳定窗口阈值仍需任务 1.11 基于本地测量固化；受信身份签名机制仍需任务 1.8 结合 OAC 本地/测试拓扑确定。
