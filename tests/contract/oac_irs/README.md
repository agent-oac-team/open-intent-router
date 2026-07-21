# OAC/IRS 到 OIR 迁移契约基线

本目录是 `replace-irs-with-oir-oac-adapter` 的阶段 0 可执行输入，也是后续 Adapter Contract Tests 和 Shadow Replay 的版本化总体。

## 内容

- `irs-baseline/v1/openapi`：IRS 22 个迁移方法的运行时 OpenAPI 与逐 operation Schema。
- `irs-baseline/v1/success`：每个迁移方法至少一个脱敏成功请求/响应。
- `irs-baseline/v1/errors`：验证、鉴权、权限、无命中、Provider、超时和截断样本。
- `oac-sequences/v1`：OAC 新请求、直接回复、Agent、Plan、Event 和重试时序。
- `golden/route/v1`：8 种 action 和权限/意图维度 Route Golden Dataset。
- `golden/knowledge/v1`：Search/Grouped/Read/Assets/Chunks 与 6 份原始文件语义基线。
- `identity/v2`：OAC Host HMAC current-only Envelope、credential profiles、重放防护和 key rotation 契约。
- `execution-ticket/v1`：Ticket Wire/运行态字段位置和向后兼容规则。
- `migration-records`：接口差异、URL 矩阵、IRS 性能基线与切换阈值。
- `baseline-index.json`：以上文件的 SHA-256 和数量清单。

## 校验入口

```bash
.venv/bin/python scripts/build_oac_irs_contract_index.py --check
.venv/bin/python scripts/validate_oac_sequence_fixtures.py
.venv/bin/python scripts/validate_route_golden_dataset.py
.venv/bin/python scripts/validate_knowledge_golden_dataset.py
.venv/bin/python scripts/validate_identity_signature.py
.venv/bin/python scripts/validate_execution_ticket_contract.py
.venv/bin/python scripts/validate_migration_thresholds.py
node scripts/validate_identity_signature.mjs
go run scripts/validate_identity_signature.go
```

Contract Test 实现必须从 `baseline-index.json` 读取已登记 fixture。新增或修改 Legacy 契约时，必须同步更新 fixture/dataset、重新生成索引并执行 100% replay；禁止只修改 Adapter Schema 而不更新验收总体。
