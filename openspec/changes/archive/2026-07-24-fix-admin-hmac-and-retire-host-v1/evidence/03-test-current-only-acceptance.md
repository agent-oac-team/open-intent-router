## 环境

- 日期：2026-07-23
- OAC Go：测试环境服务，端口 8182
- OIR Host：测试环境服务，端口 8280
- IRS 对照服务：既有测试服务，端口 8084
- 证据不包含 Secret、JWT、请求签名或业务正文。

## Current-only 传输回放

| Profile / 负向场景 | 结果 |
| --- | --- |
| `oac_user` V2 Route | 5 次签名请求经 OAC Go 成功完成 |
| `oac_admin` V2 Registry | 2 次签名请求经 OAC Go 成功完成 |
| `coze_workflow` V2 Knowledge read | 1 次签名请求经 OAC Go 成功完成 |
| 无效 V1 签名 | 401 `host_authentication_failed` |
| 无效 V2 签名 | 401 `host_authentication_failed` |
| 普通用户访问 Admin endpoint | 403 |
| 无效 Coze ingress token | 401 |
| Coze Knowledge write | 403 |

部署后的 Capability 报告 current signature version 为 `v2`、accepted versions 为 `[v2]`、
V1 compatibility disabled，且 Central Route 要求 V2。V1 成功使用量为 0。
因此，本次测试回放完成任务 8.2 和 9.9；不包含生产变更或 OpenSpec 归档授权。
