## Context

`open-intent-router` 目前已经具备较好的本地验收基础：后端使用 pytest，前端使用 Vitest 与 Vite production build，OpenSpec change 记录了 Router、Plan、Context、UI 等能力演进，`pyproject.toml` 也已有 ruff 规则配置。但这些检查尚未形成仓库级自动门禁，开发者或 AI agent 需要凭记忆手动运行。

本 change 的核心是把现有验收命令、静态巡检和契约校验沉淀为可复现的 CI/CD 兜底。它不改变 Router、PlanExecutor、Registry、LLM Provider 或前端测试台的业务行为，只建立工程基础设施：仓库中的 workflow、依赖分组、检查脚本、文档规则，以及需要在 GitHub 仓库设置中启用的 required checks。

## Goals / Non-Goals

**Goals:**

- 为 PR / merge 建立最小但可靠的 blocking CI：后端测试、前端测试、前端构建和 Python 静态检查。
- 固化 lint / audit 等 CI 依赖，确保干净环境可以安装并运行验收命令。
- 将 OpenSpec strict validate 纳入涉及 change 的验收流程。
- 建立 scheduled / on-demand inspection，用于 secret scan、依赖漏洞、依赖漂移和后续性能基线。
- 明确核心 contract 回归范围：Router、Plan、Invocation、Registry、Admin Security、Context Pack、LLM 输出兼容和前端测试 UI。
- 记录 GitHub 仓库设置中需要启用的分支保护和 required status checks。

**Non-Goals:**

- 不修改核心业务逻辑，除非 CI 暴露真实缺陷。
- 不在首版引入真实外部 LLM、真实 API Key 或外部业务系统依赖。
- 不要求首版搭建完整部署流水线或生产发布系统。
- 不要求所有巡检都在 PR 阶段阻塞；慢检查和噪声较高的检查可以先 scheduled 或 advisory。
- 不引入重型测试平台、私有 runner 或复杂矩阵，除非后续性能或数据库兼容需求证明必要。

## Decisions

### Decision 1: PR blocking 先覆盖已有稳定验收

首版 CI workflow 以已有稳定命令为主：

- `.venv/bin/python -m pytest` 或等价的 Python 环境内 `python -m pytest`
- `cd web && npm ci`
- `cd web && npm run test`
- `cd web && npm run build`
- `ruff check .`
- `ruff format --check .`

这些命令覆盖当前最核心的回归面，同时运行时间短，适合作为每个 PR 的强制门禁。

替代方案是一次性加入数据库矩阵、依赖漏洞扫描、性能基线和 e2e 浏览器测试。该方案覆盖更宽，但容易让首版 CI 变慢、变脆，阻碍团队形成稳定使用习惯。

### Decision 2: CI 依赖必须由仓库声明，而不是依赖本机环境

当前 `pyproject.toml` 已有 ruff 配置，但本地虚拟环境未必安装 ruff。首版应新增 `dev` 或 `ci` optional dependency，至少包含 ruff，后续可以纳入 pip-audit 等巡检工具。前端使用 `web/package-lock.json` 和 `npm ci`，避免 CI 与本地 npm 解析结果漂移。

替代方案是在 GitHub Actions 中临时 `pip install ruff`。该方案能快速工作，但依赖和版本没有进入项目契约，不利于本地复现。

### Decision 3: OpenSpec 校验按变更触发，归档前必须手动或自动确认

OpenSpec strict validate 应覆盖两类场景：

- PR 中修改了 `openspec/changes/<change-name>/` 时，对对应 change 运行 `openspec validate <change-name> --strict`。
- change 完成并准备归档前，必须运行同一条 strict validate。

如果自动识别变更名称的脚本在首版实现成本过高，可以先提供文档化命令和 CI 的 on-demand/manual job，再迭代为自动检测。

替代方案是在每次 PR 都 validate 所有 historical changes。该方案简单但不必要，且已完成历史 change 可能被归档策略影响。

### Decision 4: 巡检分为 blocking、advisory 和 scheduled

建议分层：

- Blocking: pytest、frontend test、frontend build、ruff check、ruff format check、OpenSpec changed validation。
- Advisory: secret scan、dependency audit、dependency freshness，在初期以告警为主。
- Scheduled: 每日或每周运行依赖漏洞、依赖漂移、可选性能基线、可选 PostgreSQL integration。

当某类巡检噪声降低并有明确 owner 后，再升级为 blocking。

替代方案是所有巡检一律阻塞。该方案安全感强，但容易因第三方 advisory、依赖库漏洞争议或审计噪声拖慢正常开发。

### Decision 5: CI 不依赖真实 `.env` 和真实外部服务

CI 应使用 mock LLM、本地文件 registry、SQLite 或内存仓库等默认可控配置。任何 OpenAI-compatible / DeepSeek key、Admin Token、数据库密码或第三方系统凭证都不能出现在 workflow、日志、fixture 或文档示例中。

需要外部服务的检查应另设 protected environment 或 scheduled integration job，并通过 GitHub Secrets 注入，且默认不影响普通 PR。

替代方案是在 CI 中跑真实 LLM 或真实业务系统 smoke test。该方案能暴露集成问题，但会引入成本、波动、凭证风险和不可复现结果，不适合作为开源核心项目的基础门禁。

### Decision 6: Contract regression 先用现有单测固定核心行为，再逐步补 golden fixture

首版不必为了 CI 改大量测试。先把现有 63 个后端测试和前端测试接入门禁，再针对高风险路径补充：

- API response golden fixture。
- PlanExecutor dependency / pause / resume / policy matrix。
- OpenAI-compatible LLM legacy and malformed output normalization。
- Context Pack budget trimming and log redaction。
- Admin Security local / non-local matrix。

替代方案是先设计完整 contract testing framework。该方案更系统，但会推迟最重要的第一步：让已有回归每天自动运行。

### Decision 7: GitHub branch protection 是实现门禁的最后一环

仓库内 workflow 只能产生 checks，真正阻止不合格合并需要在 GitHub repository settings 中配置 branch protection：

- 保护 `main` 或团队实际合并分支。
- Require status checks to pass before merging。
- 选择 CI workflow 中稳定的 job 名作为 required checks。
- 视团队需要启用 require pull request review。

这一步通常不能仅靠代码提交完成，需要仓库管理员在 GitHub UI 或 GitHub CLI 中配置。

## Risks / Trade-offs

- [Risk] CI 首次接入后发现本地隐式依赖或 flaky test。 -> Mitigation: 先以现有稳定命令为门禁，发现问题时修复测试隔离和依赖声明。
- [Risk] ruff 引入后暴露大量历史风格问题。 -> Mitigation: 使用当前 `pyproject.toml` 已配置规则，必要时先以最小范围修复或逐步收紧。
- [Risk] dependency audit 噪声过高。 -> Mitigation: 初期 advisory / scheduled，不立即阻塞 PR；明确高危漏洞处理策略后再升级。
- [Risk] OpenSpec validate 自动识别 change 名称复杂。 -> Mitigation: 首版可提供手动 workflow 或文档化命令，再迭代 changed-files 脚本。
- [Risk] GitHub branch protection 未配置导致 CI 只提示不阻塞。 -> Mitigation: 在 tasks 和文档中明确仓库管理员必须配置 required checks。

## Migration Plan

1. 新增 CI / dev 依赖分组，让 ruff 等检查工具可在干净环境安装。
2. 新增 GitHub Actions workflow，运行 Python 依赖安装、pytest、ruff、前端 npm ci、Vitest 和 build。
3. 为 OpenSpec validate 增加文档化验收命令，必要时新增 manual job 或 changed-files 自动检测。
4. 新增 secret scan / dependency audit 的 scheduled 或 advisory job。
5. 更新 README / AGENTS / docs 中的验收命令和 CI 分层说明。
6. 在 GitHub 仓库设置中启用 branch protection 和 required status checks。
7. 后续按风险补充 golden contract fixture、性能基线和数据库矩阵。

Rollback 策略：如 CI workflow 阻塞正常开发，可先临时取消 branch protection 中的 required check，保留 workflow 作为 advisory；仓库代码层面回滚 `.github/workflows/*` 和依赖声明即可恢复到纯本地验收模式。

## Open Questions

- 首版 GitHub 默认保护分支是 `main`、`master`、`dev` 还是当前团队实际合并分支？
- secret scan 采用 GitHub Advanced Security / secret scanning、gitleaks，还是先依赖 GitHub 原生告警？
- dependency audit 是否在首版阻塞高危漏洞，还是先 scheduled advisory？
