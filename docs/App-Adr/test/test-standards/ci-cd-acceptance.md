# CI/CD 验收与自动化巡检

本文档记录 `open-intent-router` 的第 4 层验收：自动化回归 + 自动化巡检。目标是让每次合并前的基础质量门禁可复现，并把安全、依赖和后续性能基线放到可持续巡检路径中。

## 本地验收命令

后端回归：

```bash
.venv/bin/python -m pytest
```

Python 静态检查：

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

前端回归与生产构建：

```bash
cd web
npm ci
npm run test
npm run build
```

OpenSpec 变更校验：

```bash
openspec validate <change-name> --strict
```

本次变更的校验命令：

```bash
openspec validate add-ci-regression-and-inspection --strict
```

如果本地提示 `openspec: command not found`，说明当前 shell 没有安装或没有找到 OpenSpec CLI。当前项目使用的 CLI 来源是 npm 包 `@fission-ai/openspec`，CI 中会安装固定版本：

```bash
npm install -g @fission-ai/openspec@1.3.1
```

## PR Blocking Checks

`.github/workflows/ci.yml` 在 `pull_request`、`push` 和手动触发时运行，目标分支为 `main`、`master`、`dev`。

首版建议设置为 required status checks 的 job：

- `backend-regression`
- `python-static-checks`
- `frontend-regression`
- `openspec-validation`

这些检查覆盖：

- 后端 clean install 后运行 `python -m pytest`。
- Python `ruff check .` 和 `ruff format --check .`。
- 前端基于 `web/package-lock.json` 执行 `npm ci`、`npm run test`、`npm run build`。
- 修改 `openspec/changes/<change-name>/` 时运行 `openspec validate <change-name> --strict`。

CI 使用 `APP_ENV=local`、`STORAGE_BACKEND=memory`、`REGISTRY_BACKEND=file`、`ROUTER_LLM_PROVIDER=mock` 等安全默认值，不依赖 `.env`、本地 `.venv`、`web/node_modules`、真实 LLM Key 或外部数据库。

## Advisory And Scheduled Inspections

`.github/workflows/inspection.yml` 默认每周一运行，也支持 `workflow_dispatch` 手动运行。

首版 advisory 检查包括：

- `backend-dependency-audit-advisory`：安装 `.[audit]` 后运行 `pip-audit --local`。
- `frontend-dependency-audit-advisory`：基于 `web/package-lock.json` 运行 `npm ci` 和 `npm audit --audit-level=high`。
- `dependency-freshness-advisory`：报告 Python 与前端依赖可更新版本，不阻塞普通 PR。
- `performance-baseline-placeholder`：预留轻量性能基线入口；当 `tests/performance` 或 `benchmarks` 建立后再补具体命令。

这些检查初期不建议配置为 required checks。等漏洞审计噪声、处理 owner 和升级策略稳定后，再把高危漏洞检查升级为阻塞门禁。

## Dependabot

`.github/dependabot.yml` 已配置三个生态的每周更新提醒：

- Python `pip`：仓库根目录。
- npm：`/web`。
- GitHub Actions：仓库根目录。

Dependabot 只负责创建依赖更新 PR，不等于漏洞审计门禁。依赖更新 PR 仍需要通过 `CI Regression Gate`。

## GitHub Branch Protection 设置

仅提交 workflow 文件不会阻止合并。要让 CI 真正成为门禁，需要仓库管理员在 GitHub 仓库设置中配置分支保护。

建议操作：

1. 打开 GitHub 仓库 `Settings`。
2. 进入 `Branches`。
3. 在 `Branch protection rules` 中新增或编辑规则。
4. Branch name pattern 按团队实际合并分支填写，例如 `main`、`master` 或 `dev`。
5. 勾选 `Require status checks to pass before merging`。
6. 选择 required checks：
   - `backend-regression`
   - `python-static-checks`
   - `frontend-regression`
   - `openspec-validation`
7. 建议同时开启 `Require pull request before merging`，再按团队需要配置 review 数量。

如果团队实际使用多个长期分支，请分别创建规则，或使用适合仓库策略的 branch pattern。

## GitHub 原生 Secret Scanning 设置

本项目首版采用 GitHub 原生 Secret Scanning，不引入 gitleaks workflow。原因是凭证泄露扫描属于仓库安全能力，启用后可以减少 workflow 中误打印敏感值的风险，也便于在 GitHub 安全面板统一处理。

建议操作：

1. 打开 GitHub 仓库 `Settings`。
2. 进入 `Code security and analysis`。
3. 找到 `Secret scanning`，点击启用。
4. 如果页面提供 `Push protection`，建议同步启用，用于在 push 阶段阻止已识别 secret。
5. 在 `Security` 面板查看 secret scanning alerts。
6. 处理告警时只记录文件路径、凭证类型和修复动作，不在 issue、PR comment 或日志中粘贴完整 secret 值。

仓库中的 `.env.example` 和 `.env.deepseek.example` 只能保留占位值，例如 `replace-with-real-key`。真实 API Key、Token、数据库密码、Authorization Header 都不能进入 tracked files、测试 fixture 或文档示例。

## OpenSpec 失败处理

`openspec-validation` 失败时先看失败的 change 名称，再本地运行：

```bash
openspec validate <change-name> --strict
openspec status --change <change-name>
```

常见原因：

- `proposal.md`、`design.md`、`tasks.md` 缺失或未完成。
- `specs/**/spec.md` 缺少 `## ADDED Requirements`、`## MODIFIED Requirements` 等合法分节。
- requirement 缺少 scenario。
- tasks 没有按 OpenSpec schema 使用 checkbox。
- 准备归档前没有再次 strict validate。

修复后重新提交，让 `openspec-validation` 重新运行。

## 契约回归范围

当前回归套件应持续覆盖这些隐式契约：

- Router：候选裁剪、固定问覆盖、低置信澄清、缺失输入澄清、RouteResponse 响应形状。
- Registry：角色、租户、启用状态和 access policy。
- Plan：`plan` 是多意图主契约，`show_plan` 只是兼容行为；PlanExecutor 覆盖依赖顺序、暂停、恢复、失败和完成状态。
- Invocation：`route-and-invoke`、`route-and-execute`、单 Agent 调用结果和错误语义。
- Admin Security：本地 loopback、非 local token 要求、配置 token 后强制校验。
- LLM Compatibility：OpenAI-compatible malformed output、legacy output、fallback plan 和占位 key 拒绝。
- Context Pack：预算裁剪、摘要占位、metadata 脱敏、route log 不持久化完整 raw context。

新增或修改这些路径时，必须补充或更新对应测试，并运行本地验收命令。
