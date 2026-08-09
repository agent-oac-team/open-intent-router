# ci-regression-gate Specification

## Purpose
TBD - created by archiving change add-ci-regression-and-inspection. Update Purpose after archive.
## Requirements
### Requirement: CI runs backend regression before merge
The repository SHALL provide a CI job that installs Python project dependencies in a clean environment and runs the full backend pytest suite before code can be merged through the protected branch workflow.

#### Scenario: Backend tests pass
- **WHEN** a pull request changes backend code, shared schemas, configuration, docs that affect commands, or tests
- **THEN** CI runs the backend pytest suite and reports a passing backend regression check before merge

#### Scenario: Backend tests fail
- **WHEN** any backend pytest test fails in CI
- **THEN** the backend regression check fails and the protected branch does not allow merge while the check is required

### Requirement: CI runs frontend regression and build before merge
The repository SHALL provide CI jobs that install frontend dependencies with the lockfile, run the frontend test suite, and run the production build before frontend changes can be merged through the protected branch workflow.

#### Scenario: Frontend tests pass
- **WHEN** a pull request changes files under `web/`
- **THEN** CI runs `npm ci`, `npm run test`, and `npm run build` from the `web` directory

#### Scenario: Frontend build fails
- **WHEN** TypeScript checking or Vite production build fails in CI
- **THEN** the frontend build check fails and the protected branch does not allow merge while the check is required

### Requirement: CI runs Python static checks
The repository SHALL run Python lint and format checks with versions declared by the project rather than relying on a developer machine.

#### Scenario: Lint check passes
- **WHEN** a pull request changes Python code, tests, or Python configuration
- **THEN** CI runs `ruff check .` using a project-declared dependency version

#### Scenario: Format check fails
- **WHEN** Python formatting differs from the configured formatter output
- **THEN** CI reports a failed format check before merge

### Requirement: CI environment is reproducible
The CI workflow SHALL install dependencies from repository-declared configuration and MUST NOT depend on local virtual environments, uncommitted files, or developer-specific `.env` values.

#### Scenario: Clean checkout
- **WHEN** CI starts from a clean repository checkout
- **THEN** it can install backend and frontend dependencies using committed project metadata and lockfiles

#### Scenario: Local environment absent
- **WHEN** `.venv`, `web/node_modules`, local databases, or `.env` files are absent from CI
- **THEN** the regression jobs still run using safe default test configuration

### Requirement: Required status checks enforce the gate
The project SHALL document the GitHub branch protection settings needed to make CI checks block merges into the protected branch.

#### Scenario: Branch protection configured
- **WHEN** repository administrators configure required status checks for the protected branch
- **THEN** pull requests cannot merge until the selected backend, frontend, build, and static checks pass

#### Scenario: Workflow exists without branch protection
- **WHEN** CI workflows exist but required checks are not enabled in GitHub settings
- **THEN** the project documentation identifies that CI is advisory and does not yet enforce merge blocking

