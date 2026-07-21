## 失败基线

本变更开始时，OAC `centralAdminRegistryRequest` 只发送
`X-Admin-Sync-Token`，没有发送 `X-OIR-Host-*` HMAC Header。真实边界的稳定结果为：

1. OIR Registry 的 `get_trusted_host_identity` 返回 `401 host_authentication_failed`。
2. OAC `/api/central-agent-registry` 将 downstream 非 2xx 投影为 `502`。
3. Client 捕获读取失败，记录 `Failed to load central registry, using fallback map`，并回退到静态 `CENTRAL_AGENT_ROUTE_MAP`。

自动化证据分别位于 OIR `test_registry_token_only_request_fails_real_host_verifier`
和 OAC `TestCentralAgentRegistryTokenOnlyFailureProjectsToBadGateway`。测试不得覆写
`TrustedHostIdentity`，downstream 也不得使用无条件接受 Header 的 Stub。

## 调用与凭证矩阵

| 边界 | 调用方 | downstream path / method | 凭证类别 | 变更前版本 | 最终版本 |
| --- | --- | --- | --- | --- | --- |
| Browser -> Go | OAC Client | `GET /api/central-agent-registry` | Browser JWT | 不适用 | 不适用 |
| Go -> OIR | Registry enabled list | `GET /api/v1/admin/agent-registry?enabled_only=true` | `oac_admin` | 缺失 HMAC | V2 |
| Go -> OIR | Registry management list | `GET /api/v1/admin/agent-registry` | `oac_admin` | 缺失 HMAC | V2 |
| Go -> OIR | Registry create | `POST /api/v1/admin/agent-registry` | `oac_admin` | 缺失 HMAC | V2 |
| Go -> OIR | Registry update | `PUT /api/v1/admin/agent-registry/{escaped_agent_id}` | `oac_admin` | 缺失 HMAC | V2 |
| Go -> OIR | Registry enabled | `PATCH /api/v1/admin/agent-registry/{escaped_agent_id}/enabled` | `oac_admin` | 缺失 HMAC | V2 |
| Go -> OIR | Registry delete | `DELETE /api/v1/admin/agent-registry/{escaped_agent_id}` | `oac_admin` | 缺失 HMAC | V2 |
| Browser -> Go | OAC Admin UI | `/api/admin/knowledge/*` | Browser JWT | 不适用 | 不适用 |
| Go -> OIR | Knowledge Admin | `/api/v1/admin/knowledge/*` 与允许的 read path | `oac_admin` | V1 + role groups | V2 Admin profile |
| Coze -> Go | Coze Workflow | `/api/v1/knowledge/*` read-only allowlist | ingress Bearer token | 不适用 | 不适用 |
| Go -> OIR | Coze Knowledge | 同一只读 path | `coze_workflow` | V1 | V2 service profile |
| Browser -> Go | OAC Client | `/api/central/*` | Browser JWT | 不适用 | 不适用 |
| Go -> OIR | Agent Route | `/api/v1/central/*` | `oac_user` | Route 已为 V2，其他旧路径可为 V1 | V2 User profile |

## `X-Admin-Sync-Token` 的两个边界

- Browser/Next -> OAC Go：若既有 Knowledge BFF 仍使用该 Token，它只是 ingress
  认证机制，必须在 Go 边界终止。
- OAC Go -> OIR：迁移初期可为 wire compatibility 附带，但 OIR 不读取、不投影、
  不授权；最终 Registry downstream 不再发送或配置该 Token。

浏览器的 `Authorization`、任何入站 `X-OIR-Host-*`、Coze ingress Bearer token
均不得成为 Go -> OIR 的授权证据。最终 downstream 身份只由 Go 使用当前密钥和
服务端验证后的 actor/profile 重新签发。
