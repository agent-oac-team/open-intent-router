# OIR Web 测试台作用域规则

本文件适用于 `web/**`，补充根 `AGENTS.md`。

- `web/` 是本地开发与验收测试台，不是生产管理后台。不要在此实现生产身份、权限事实源或密钥管理。
- 每个 ConversationTurn 只展示该轮真实 Route / Invocation / Context / Memory / Knowledge / Evidence 数据；不得用全局 Debug 数据或上一轮状态反推本轮结果。
- 请求进行中只展示已知等待状态，不用定时器伪造后端正在执行的内部阶段。
- API 类型与后端 Schema 同步；状态枚举由后端契约驱动，不在前端猜测新的终态。
- 涉及 Memory / Knowledge / identity 的原始 JSON 和 metadata 必须使用统一脱敏展示。
- 保持桌面和窄屏布局可用、文本不溢出、可操作控件可键盘访问；运行图详情遵循现有真实性边界。
- 修改后至少运行 `npm run test` 和 `npm run build`，并同步 `docs/App-Desc/interfaces/visual-test-ui.md` 或 `docs/App-Desc/interfaces/routing-journey-visualization.md`。
