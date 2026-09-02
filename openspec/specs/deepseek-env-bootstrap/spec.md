# deepseek-env-bootstrap Specification

## Purpose
TBD - created by archiving change add-visual-test-ui. Update Purpose after archive.
## Requirements
### Requirement: DeepSeek OpenAI-compatible environment template

The project SHALL provide documented local environment configuration for using DeepSeek through the existing OpenAI-compatible LLM provider.

#### Scenario: Configure DeepSeek routing

- **WHEN** a developer follows the DeepSeek environment instructions
- **THEN** the backend can be configured with `ROUTER_LLM_PROVIDER=openai_compatible`, `ROUTER_LLM_API_STYLE=responses`, `ROUTER_LLM_MODEL=deepseek-v4-flash`, `ROUTER_LLM_BASE_URL=https://api.deepseek.com`, and `ROUTER_LLM_API_KEY`

#### Scenario: Preserve legacy Chat Completions deployments

- **WHEN** `ROUTER_LLM_API_STYLE` is omitted
- **THEN** the backend continues to use `chat_completions` so existing OpenAI-compatible deployments do not change behavior

#### Scenario: Parse Responses output items safely

- **WHEN** a Responses payload contains reasoning or tool output before its assistant message
- **THEN** the backend extracts structured JSON from `output_text` content without assuming the first output item is the answer

#### Scenario: Preserve provider abstraction

- **WHEN** DeepSeek configuration is added
- **THEN** the backend still uses the generic OpenAI-compatible client and does not require a DeepSeek-specific hard dependency

### Requirement: Secret-safe env files

The project SHALL avoid committing real API keys while still making local setup straightforward.

#### Scenario: Example env is committed

- **WHEN** environment examples are committed
- **THEN** API key fields contain placeholders and no real secret values

#### Scenario: Local env is ignored

- **WHEN** a developer creates `.env` with a real DeepSeek API key
- **THEN** git ignore rules prevent the local secret file from being committed

### Requirement: Legacy DeepSeek field mapping documentation

The documentation SHALL explain how original project DeepSeek fields map to open-intent-router fields.

#### Scenario: Read migration mapping

- **WHEN** a developer references the original project's `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, and `DEEPSEEK_BASE_URL`
- **THEN** the docs show the corresponding `ROUTER_LLM_API_KEY`, `ROUTER_LLM_MODEL`, and `ROUTER_LLM_BASE_URL` fields
