# OAC 到 Adapter 受信签名拓扑结论

## 结论

Host 内部传输只使用 `OIR-HOST-V2` HMAC-SHA256，不接受 V1、浏览器 JWT
直传或旧同步 Token。V2 canonical 对 User、Admin、Coze 完全相同，身份差异由
credential profile 的严格字段规则表达。

| Credential class | Principal | Claims / policy | Roles | Groups | Active Bundle |
| --- | --- | --- | --- | --- | --- |
| `oac_user` | user | `oac-principal-v1` / `oac-authz-v1` | 必填 | 禁止 | 必填 |
| `oac_admin` | user | `oac-admin-principal-v1` / `oac-control-v1` | 禁止 | 禁止 | 禁止 |
| `coze_workflow` | service | `oac-service-principal-v1` / `oac-readonly-v1` | 禁止 | 禁止 | 禁止 |

## 信任边界

- 浏览器 JWT、`X-OAC-Edition` 和 Coze ingress token 只在 OAC Go 边界使用，
  不转发到 OIR，也不作为 Adapter 最终授权证据。
- OAC Go 根据当前数据库中的 subject、role、status、approval status 验证后，
  使用对应 current key 生成 V2 Host credential。
- Admin subject 是真实操作者 ID，但不携带 role/groups/Bundle；Registry audit
  使用该 subject。
- Adapter 固定 `tenant_id=oac`。Body/query/Header 中的自报身份不能覆盖签名身份。

## 重放、轮换与环境隔离

- timestamp 允许正负 60 秒时钟偏差；nonce 至少 128 bit，并在 120 秒内按
  `key_id + nonce` 原子消费，重复请求拒绝。
- signer 只使用 current key；User 可在有界轮换窗口接受 previous key，Admin/Coze
  key 不得跨 credential class。
- local/test audience 与密钥均隔离。错误 key、audience、profile、正文 hash、签名、
  timestamp 或 nonce 统一返回 `401 host_authentication_failed`。
- capability 最终只报告 current/accepted=`v2` 和低基数汇总计数，不暴露 key ID、
  subject、完整 claims、Token、Ticket、签名或正文。

## 部署映射

| 调用方 | 受信身份来源 | 签名位置 | Credential profile |
| --- | --- | --- | --- |
| OAC Go Central Proxy | 当前用户事实 + 当前 edition | Go 出站代理 | `oac_user` |
| OAC Go Registry/Knowledge Admin | 当前 operator/admin 事实与真实 actor | Go 出站代理 | `oac_admin` |
| Coze Workflow | 独立 ingress token 对应的固定 service principal | Go 只读代理 | `coze_workflow` |

签名实现仅位于 OAC Host Adapter identity 模块和 OAC Go signer，不进入 OIR Core
Schema、service、prompt 或公共配置。
