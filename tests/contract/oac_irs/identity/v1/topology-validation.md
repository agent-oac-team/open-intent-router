# OAC 到 Adapter 受信签名拓扑结论

## 结论

首期采用 `oir-host-signature/v1` HMAC-SHA256 服务签名，不采用浏览器 JWT 直传或 mTLS。

原因：

- OAC Go 已在 `authMiddleware` 验证 JWT，并覆盖 `X-User-ID/X-Username/X-User-Role`，适合在代理出站前将可信身份签入请求。
- OAC Next.js Knowledge Route 使用浏览器 JWT 调用 OAC Go，由 Go 集中执行 Admin HMAC 签名，不需要浏览器或 Next.js 持有签名密钥。
- 当前本地/测试拓扑没有客户端证书分发、mTLS 校验或现成网关签名设施；首期引入 mTLS 会新增证书生命周期和部署依赖。
- Go 标准库、Node 标准库和 Python 标准库对同一版本化测试向量产生一致签名。

## 信任边界

- 浏览器 `Authorization` 只用于 OAC 自身认证，不转发给 OIR Core，也不作为 Adapter 最终授权证据。
- Go/Next 在服务端解析或获取 OAC 当前用户后，签入 user ID、允许列表映射后的 groups 和 credential class。
- Adapter 固定注入 `tenant_id=oac`。Body/query 中的 `tenant_id/user_id/user_tags/consumer` 只保留兼容形状，不能覆盖签名身份。
- Coze 使用独立入口 token 调用 OAC Go 只读 Knowledge ingress，由 Go 映射为独立 `coze_workflow` HMAC 服务凭证；不得持有 OAC user/admin 的签名密钥。

## 重放与轮换

- timestamp 允许正负 60 秒时钟偏差；超窗拒绝。
- nonce 至少 128 bit，按 `key_id + nonce` 原子存储 120 秒；重复拒绝。
- signer 只用 current key；verifier 可同时接受 current/previous key ID，轮换重叠 24 小时。
- local/test audience 不同，密钥也必须不同，防止跨环境重放。
- 未知 key ID、错误 audience、正文 hash 不符、签名不符、过期 timestamp 或重复 nonce 均返回统一认证失败，不暴露具体密钥状态。

## 部署映射

| 调用方 | 身份来源 | 签名位置 | audience | credential class |
| --- | --- | --- | --- | --- |
| OAC Go Central Proxy | 已验证 OAC JWT 注入的 user ID/role | Go 出站代理 | 按 local/test 配置 | `oac_user` |
| OAC Go Knowledge Admin Proxy | `adminAuthMiddleware` 验证的 user/role | Go 出站代理 | 按 local/test 配置 | `oac_admin` |
| OAC Next Knowledge Route | 服务端调用 Go `/api/auth/me` 得到的 user/role | OAC Go 出站代理 | 按 local/test 配置 | `oac_admin` |
| Coze Workflow | 独立 ingress token | OAC Go 只读 ingress + Adapter credential verifier | test | `coze_workflow` |

## 后续实现约束

签名实现放在 OAC Host Adapter identity 模块和 OAC Go signer 中，不进入 OIR Core Schema、Prompt 或配置。真实密钥只通过环境变量或 secret store 注入，日志只允许记录 key ID、audience、认证结果和脱敏 principal reference。
