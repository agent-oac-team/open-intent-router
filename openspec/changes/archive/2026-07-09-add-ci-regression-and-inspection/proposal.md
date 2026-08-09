## Why

当前项目已经有后端 pytest、前端 Vitest / build、OpenSpec 变更文档和部分静态规则，但这些验收还依赖人工记忆，没有形成合并前的自动化兜底。AI 辅助开发会快速修改 Router、Plan、Invocation、Registry、Security 和 Context 等隐式契约，因此需要把自动化回归和自动化巡检固化为 CI/CD 门禁。

## What Changes

- 新增 CI 回归门禁：PR / merge 前自动运行后端全量测试、前端测试、前端生产构建和基础静态检查。
- 固化项目验收命令和开发依赖，确保干净环境可以复现本地验收结果。
- 新增 OpenSpec 变更校验规则：涉及 OpenSpec change 的 PR 或归档前必须运行 `openspec validate <change> --strict`。
- 新增自动化巡检能力：覆盖代码风格、格式、secret 泄露、依赖漏洞和依赖版本漂移的分层检查。
- 新增契约回归策略：将 Router、Plan、Invocation、Registry、Admin Security、Context Pack 等高风险隐式 contract 纳入重点回归范围。
- 定义 PR blocking、scheduled inspection 和 release preflight 三类运行层级，避免把所有慢检查都压到每次提交。
- 不改变核心业务逻辑；只有在 CI 揭示真实问题时，才修复相关代码或测试。

## Capabilities

### New Capabilities

- `ci-regression-gate`: PR / merge 前必须通过的后端、前端、构建和静态检查门禁。
- `automated-inspection`: 定时或按需执行的代码风格、安全、依赖和运行基线巡检。
- `contract-regression`: 面向核心 API、OpenSpec change 和隐式业务契约的回归验收策略。

### Modified Capabilities

- None. 当前仓库尚未归档 `openspec/specs/` 基线，本 change 以新增工程基础设施能力 spec 记录后续实现契约。

## Impact

- Repository infrastructure:
  - 新增 `.github/workflows/ci.yml` 作为 PR / push 的基础 CI 门禁。
  - 可选新增 `.github/workflows/inspection.yml` 或同一 workflow 的 scheduled job。
  - 可选新增 `.github/dependabot.yml` 管理依赖更新提醒。
- Python project configuration:
  - 更新 `pyproject.toml`，补充 lint / audit 等 dev 或 ci 依赖分组。
  - 保持 pytest 测试命令和当前 FastAPI / Pydantic 运行方式兼容。
- Frontend project configuration:
  - 复用 `web/package-lock.json` 和 `npm ci`，保持前端测试与生产构建可复现。
  - 必要时补充 npm 脚本以统一 CI 命令。
- OpenSpec and docs:
  - 更新 README / AGENTS 或项目文档中的验收命令。
  - 将 OpenSpec strict validate 纳入变更完成和归档前检查。
- Tests:
  - 初期复用现有测试套件。
  - 后续为核心 API 响应、Plan 执行策略、LLM Provider 兼容输出和安全日志补充契约回归 fixture。
