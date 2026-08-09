# Test Skills

当前测试执行以仓库内的 pytest、contract fixture、replay / migration 脚本和 OpenSpec Apply 任务为主，尚未形成需要单独包装的 OIR 测试技能。

本目录作为测试技能的正式入口。新增技能时必须同时封装：

1. 环境前置与可用性检查。
2. tenant、user、database 和 collection 的数据隔离。
3. 正向、负向、失败注入和幂等验证。
4. 证据产出、脱敏、清理与失败回退。

只包装一条 pytest 命令、不处理数据域和证据生命周期的脚本，不应提炼为技能。历史测试方案和报告从 [App-Research 验收记录](../../../App-Research/README.md#测试与验收记录) 按需读取。
