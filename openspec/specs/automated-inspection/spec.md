# automated-inspection Specification

## Purpose
TBD - created by archiving change add-ci-regression-and-inspection. Update Purpose after archive.
## Requirements
### Requirement: Inspection jobs scan for secret leakage
The repository SHALL provide an automated inspection path that detects committed secrets or credential-like values before they become part of normal development history.

#### Scenario: Secret-like value detected
- **WHEN** a pull request or scheduled inspection finds an API key, token, password, authorization header, or real `.env` value in tracked files
- **THEN** the inspection reports the finding with enough file context to remediate without printing the full secret value

#### Scenario: Example environment files are scanned
- **WHEN** `.env.example`, `.env.deepseek.example`, workflow files, fixtures, or documentation examples are scanned
- **THEN** placeholder values are allowed but real credentials are reported

### Requirement: Inspection jobs audit backend dependencies
The repository SHALL provide an automated backend dependency audit that can detect known vulnerabilities in Python dependencies.

#### Scenario: Backend dependency vulnerability detected
- **WHEN** the backend dependency audit finds a known vulnerability
- **THEN** the inspection reports the affected package, installed version, vulnerability severity, and remediation guidance when available

#### Scenario: Audit is advisory during initial rollout
- **WHEN** backend dependency audit is first introduced
- **THEN** it may run as scheduled or advisory until the team marks its status check as required

### Requirement: Inspection jobs audit frontend dependencies
The repository SHALL provide an automated frontend dependency audit that can detect known vulnerabilities in npm dependencies.

#### Scenario: Frontend dependency vulnerability detected
- **WHEN** the frontend dependency audit finds a known vulnerability
- **THEN** the inspection reports the affected package, dependency path, severity, and remediation guidance when available

#### Scenario: Lockfile remains source of truth
- **WHEN** frontend dependency audit runs
- **THEN** it uses `web/package-lock.json` through `npm ci` or equivalent lockfile-aware installation

### Requirement: Inspection jobs track dependency freshness
The repository SHALL provide a periodic or on-demand path for detecting stale backend and frontend dependency versions without forcing every dependency update into the PR gate.

#### Scenario: Outdated dependency detected
- **WHEN** a scheduled freshness check finds an outdated dependency
- **THEN** the inspection reports the package name, current version, available version, and ecosystem

#### Scenario: Freshness check is not a merge gate
- **WHEN** dependency freshness check produces available update recommendations
- **THEN** it does not block unrelated pull requests unless the repository explicitly promotes that check to required status

### Requirement: Inspection jobs can host performance baselines
The repository SHALL reserve an automated inspection path for lightweight performance baseline checks of routing, context budgeting, and plan execution when such benchmarks are added.

#### Scenario: Performance baseline added
- **WHEN** a lightweight benchmark suite is introduced
- **THEN** it runs in scheduled or on-demand inspection rather than every PR unless its runtime and stability are suitable for blocking CI

#### Scenario: Baseline regression detected
- **WHEN** a benchmark exceeds the accepted regression threshold
- **THEN** the inspection reports the benchmark name, observed value, baseline value, and threshold

