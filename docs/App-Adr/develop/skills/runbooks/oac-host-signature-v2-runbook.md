# OAC Host Signature V2 运行手册

## 最终拓扑

OAC Go 是浏览器 JWT、当前用户事实、edition 选择和 Coze ingress token 的终止边界。
Go 只向 OIR 签发 `OIR-HOST-V2`：User、Admin、Coze 使用隔离 key 和严格
credential profile。OIR 不接受 V1、旧同步 Token 或浏览器 Authorization。

## 启动门禁

启动前必须同时满足：

- User/Admin/Coze current key ID 与 secret 均非空，三个 key ID 不得相同。
- audience 与环境一致，tenant 固定为 `oac`。
- profile 版本分别为 `oac-principal-v1/oac-authz-v1`、
  `oac-admin-principal-v1/oac-control-v1`、
  `oac-service-principal-v1/oac-readonly-v1`。
- Registry database 是唯一写源，飞书/IRS Registry 写入与文件恢复关闭。
- Registry/Knowledge Admin 写入没有 fallback、双写或自动重试。

任一门禁失败时，OAC/OIR 必须在监听业务流量前退出。

## 先验后签

1. Go 从 JWT 只取得 subject 定位信息。
2. Go 查询当前 users 的存在性、role、status、approval status。
3. Go 校验 operation 角色矩阵与 edition；Body/Header 自报 actor 不参与授权。
4. Go 固定最终 URL/query 与 Body wire bytes，再生成 nonce/timestamp/body hash 和 V2 HMAC。
5. OIR 先验证 key class、profile、request binding、时钟、nonce 和签名，再执行 operation policy。

## 验收顺序

1. `GET /capabilities` 必须显示 current=`v2`、accepted=`[v2]`、V1 disabled、profile catalog=`ok`。
2. Registry 执行 create/list/update/disable/enable/delete，revision 连续且 actor 为真实 OAC user ID。
3. 使用现有 Bundle/provider 新增临时 Agent，并验证相应 edition 可路由、其他 edition 不可见。
4. Knowledge Admin 执行 list、multipart upload、read/detail、delete。
5. Coze 执行 6 资产发现、`01` 资产 44 chunks Exact Read，写请求返回 403。
6. 回放 User/Admin/Coze 的坏 key/audience/profile、跨类 key、篡改、过期、replay、撤权和 V1 拒绝矩阵。
7. capability 计数只允许固定 version/class/operation/outcome 标签，不得出现 subject、resource 或 key ID。

## 提交未知

Registry/Knowledge 写请求超时或连接断开后，禁止自动重试、IRS fallback 或双写。
管理员按 stable Agent/asset ID 查询 OIR revision/audit 或资产事实：已提交则继续后续动作，
未提交才重新发起新请求。响应与日志不得输出签名、secret、Token、Ticket 或完整正文。

## 回滚

current-only 上线后不提供运行时 V1 开关。若 V2 阻塞：

1. 冻结 Registry/Knowledge Admin 写入。
2. 保存 capability、低基数计数、request correlation 与 Registry revision/audit。
3. 整体回滚到上一组已验证发布物；禁止只打开 V1 或切回 IRS/飞书。
4. 对所有提交未知写入逐条查询事实源后处置。
5. 修复后重新执行完整 current profile 正向/负向 replay，再解除写冻结。

水平扩容前必须把 nonce store 替换为共享原子存储；单进程内存 nonce store 不能作为
多副本部署能力。
