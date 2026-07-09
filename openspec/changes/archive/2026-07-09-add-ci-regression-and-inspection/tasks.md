## 1. CI Dependency Setup

- [x] 1.1 Add a project-declared backend dev or ci dependency group that includes `ruff` for lint and format checks.
- [x] 1.2 Decide whether `pip-audit` belongs in the initial dependency group or in a later inspection job.
- [x] 1.3 Verify a clean backend install can run pytest and ruff without relying on the local `.venv`.
- [x] 1.4 Keep frontend CI installation based on `web/package-lock.json` and `npm ci`.

## 2. PR Regression Gate

- [x] 2.1 Add a GitHub Actions workflow for pull requests and pushes to the protected branch.
- [x] 2.2 Add a backend test job that installs backend dependencies and runs the full pytest suite.
- [x] 2.3 Add Python lint and format check steps using `ruff check .` and `ruff format --check .`.
- [x] 2.4 Add a frontend job that runs `npm ci`, `npm run test`, and `npm run build` in `web`.
- [x] 2.5 Ensure CI uses mock/local-safe defaults and does not require `.env`, real LLM credentials, or local databases.
- [x] 2.6 Name jobs and status checks clearly so GitHub branch protection can require them.

## 3. OpenSpec Validation Gate

- [x] 3.1 Document the required `openspec validate <change-name> --strict` command for implementation and archive readiness.
- [x] 3.2 Add an initial manual or scripted path to validate the current change when OpenSpec artifacts are modified.
- [x] 3.3 If feasible, add changed-files detection to validate only touched `openspec/changes/<change-name>/` directories.
- [x] 3.4 Add failure guidance for malformed specs, missing scenarios, incomplete tasks, or inconsistent artifacts.

## 4. Automated Inspection

- [x] 4.1 Add a secret scanning path using the selected tool or GitHub-native capability.
- [x] 4.2 Ensure secret scanning reports findings without printing full secret values.
- [x] 4.3 Add backend dependency vulnerability audit as scheduled or advisory during initial rollout.
- [x] 4.4 Add frontend dependency vulnerability audit using lockfile-aware installation.
- [x] 4.5 Add a dependency freshness check path or Dependabot configuration for backend and frontend ecosystems.
- [x] 4.6 Reserve a scheduled or on-demand job for future lightweight performance baselines.

## 5. Contract Regression Coverage

- [x] 5.1 Review existing backend tests and map them to Router, Registry, Plan, Invocation, Admin Security, LLM compatibility, and Context Pack contracts.
- [x] 5.2 Add or update tests for any uncovered API response contracts that host applications depend on.
- [x] 5.3 Add or update tests for PlanExecutor dependency order, pause, resume, policy, completion, and failure behavior if gaps remain.
- [x] 5.4 Add or update tests for OpenAI-compatible LLM malformed output, legacy output, and fallback behavior if gaps remain.
- [x] 5.5 Add or update tests for log redaction and no unbounded Context Pack logging if gaps remain.
- [x] 5.6 Keep all external provider coverage based on mocks, fixtures, or local test servers rather than real credentials.

## 6. Documentation And Repository Settings

- [x] 6.1 Update README or project docs with the new local verification command set.
- [x] 6.2 Update AGENTS.md with the CI/CD acceptance expectations for future coding agents.
- [x] 6.3 Document GitHub branch protection setup and required status checks for the protected branch.
- [x] 6.4 Document which inspections are blocking, advisory, or scheduled during initial rollout.
- [x] 6.5 Document that workflow presence alone does not block merges until repository settings require the status checks.

## 7. Verification

- [x] 7.1 Run backend tests after the CI/dependency changes.
- [x] 7.2 Run frontend tests after the CI/dependency changes.
- [x] 7.3 Run frontend production build after the CI/dependency changes.
- [x] 7.4 Run ruff lint and format checks after adding the project-declared dependency.
- [x] 7.5 Run `openspec validate add-ci-regression-and-inspection --strict`.
- [x] 7.6 Confirm `openspec status --change add-ci-regression-and-inspection` reports the change as ready for implementation.
