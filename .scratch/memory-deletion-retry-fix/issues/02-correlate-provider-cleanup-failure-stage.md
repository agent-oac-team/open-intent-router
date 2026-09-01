# 02 — 关联外部清理失败的阶段证据

**What to build:** 管理和排障人员能够从安全错误码与关联事件判断一次 Memory 外部清理失败发生在扫描还是删除阶段，并准确定位对应的 Memory、Tenant 和 User，同时不持久化 Provider 异常原文。

**Blocked by:** 01 — 恢复可验证的 Memory 删除重试。

**Status:** completed

- [x] 扫描失败生成阶段化安全错误码，例如 `provider_scan_runtimeerror`；删除失败生成对应的 `provider_delete_*` 错误码。
- [x] 阶段化错误码只影响 scan/delete，不改变 add、update 或 search 的既有错误契约。
- [x] 扫描失败事件关联正确的 Memory、Tenant 和 User，可按删除任务直接查询。
- [x] 错误码和事件只保存阶段、异常类型及关联身份，不保存异常原文、连接串、凭据或 Memory 正文。
- [x] 回归测试覆盖阶段化错误码、事件关联和敏感信息不泄漏。
