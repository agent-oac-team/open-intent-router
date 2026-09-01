# 01 — 恢复可验证的 Memory 删除重试

**What to build:** 当一个待删除 Memory Item 已有外部映射时，删除重试先幂等清理该确定记录，再扫描并清理其他外部检索投影。扫描不可用时保留未完成状态，扫描恢复后的重试能够完成 Memory Deletion Completion；Provider client 初始化失败也不能污染后续重试。

**Blocked by:** None — can start immediately.

**Status:** completed

- [x] 已知外部映射在扫描前被删除，删除返回 success 或 not-found 都视为该确定动作已完成。
- [x] 扫描失败时 Index Operation 保持 retry 或 dead-letter，Canonical Data 不收口，也不报告 Memory Deletion Completion。
- [x] 后续重试再次幂等删除已知映射，并在一次成功扫描及全部逐条删除完成后收口 Canonical Data。
- [x] 不新增“已知映射已删除”等持久化阶段状态，重试正确性由删除幂等性保证。
- [x] Provider client 只有在创建和 Collection 初始化全部成功后才进入缓存；初始化失败后的下一次尝试会重新初始化。
- [x] 回归测试覆盖扫描失败的部分进展、扫描恢复后的最终完成、not-found 幂等语义和 client 初始化恢复。
