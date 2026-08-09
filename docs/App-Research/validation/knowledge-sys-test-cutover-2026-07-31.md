# knowledge_sys 测试环境切换验收

日期：2026-07-31  
环境：test  
结论：通过

## 部署结果

- `knowledge-sys-test` 以 `app.main:knowledge_app` 运行在
  `/var/www/knowledge-sys-test`，内部端口为 `8084`。
- `oir-oac-test` 运行在内部端口 `8280`，Knowledge Provider URL 指向
  `http://127.0.0.1:8084`。
- `oac-test-server` 运行在内部端口 `8182`，Knowledge URL 指向
  `http://127.0.0.1:8084`。
- `oac-central-test` 已从 PM2 删除，其原部署目录已移出 `/var/www` 并进入可恢复备份。
- Nginx 新增 `/knowledge-api/`；旧 `/central-api` 的 Knowledge 子路径经 OAC
  只读入口把旧边缘凭据转换为新的下游 JWT，并使用独立访问日志。
- OIR Central 的旧 `/central-api` 非 Knowledge 路径转发到 OIR，不再依赖旧中控。

## 身份与预算

- OAC 和 OIR 使用两把独立的 RS256 私钥，私钥位于部署源代码目录之外。
- `knowledge_sys` 只信任显式注册的 `oac`、`oir` issuer 和
  `aud=knowledge_sys`。
- `knowledge_sys` 内部检索总预算为 `10s`。
- OIR Provider 与预取外层预算均为 `12s`。
- 匿名 Knowledge Search 返回 `401`。
- OAC/Coze 只读 Search 返回 `200`，Admin 写路径返回 `403`。
- OIR 独立 JWT Search 返回 `200`。
- OAC 与 OIR 两条链路都生成结果级 `trace_id`、条目级 `item_id`，Citation
  仅要求并实际提供 `source_id`。

## 数据与 Memory 不变性

- PostgreSQL 测试数据库仍为 `oac_test`；未改数据库名。
- Knowledge PostgreSQL owner/运行角色统一为 `oac`。
- Milvus 配置与集合名保持不变；canonical Knowledge 仍有 `10` 个 assets、
  `1407` 个 chunks，未用本地 `270` 条 Golden 清单覆盖远端数据。
- OIR PostgreSQL 中固定九张 Knowledge 副本表已删除。
- OIR 两个本地 SQLite 副本中的 Knowledge 表已删除。
- `oir_knowledge_vectors` 与显式声明的
  `oir_knowledge_vectors_rehearsal` 已删除。
- `oir_memory_milvus.db`、`oir_memory_milvus_v2.db` 与 Memory 表均保留。
- 三份清理报告均为 `executed`，并记录 `memory_unchanged=true`。

## 验证与现场修复

- 本地 OIR 门禁：`1105 passed, 2 skipped`，Ruff、OpenSpec strict、
  `git diff --check` 通过。
- 本地 knowledge_sys 门禁：初始 `129 passed`；新增启动迁移回归后专项
  `5 passed`，Ruff 与格式检查通过。
- 本地 OAC 门禁：Go tests 与迁移 verifier 通过。
- 真实 PostgreSQL 清理首次演练发现 asyncpg 的 `tid` 参数必须使用二元组；
  事务自动回滚且向量恢复。修复 `dd22b08` 后重新执行成功。
- 真实旧 Trace 表缺少新增审计列；幂等启动迁移 `f7107c6` 补齐
  `tenant_id`、`principal_type`、`items_text` 后，JWT 全链路验收通过。

生产环境未在本次验收中修改。生产发布仍须在执行前获得一次新的明确确认。
