## MODIFIED Requirements

### Requirement: Registry exposes sanitized public views
The system SHALL return sanitized Agent views to non-admin callers and LLM prompts without secrets, internal URLs, auth headers, or sensitive provider configuration. Personalized available-Agent queries SHALL derive access decisions from the authenticated Principal rather than a request-body User Context.

#### Scenario: Public Agent list is requested
- **WHEN** a non-admin caller requests the documented public Agent catalog
- **THEN** the response includes sanitized Agent views and omits sensitive invocation configuration

#### Scenario: Authenticated available Agents are requested
- **WHEN** a Principal requests its available Agents
- **THEN** the Registry filters enabled definitions using the Principal-derived User Context and returns only matching Agent IDs and safe candidate summaries

#### Scenario: Available-Agent body claims permissions
- **WHEN** an available-Agent request body claims roles, groups, entitlements, tenant, or attributes not present in the Principal
- **THEN** the Registry does not use those claims to expand the available Agents

#### Scenario: LLM prompt candidates are built
- **WHEN** the router constructs candidate Agent data for the LLM
- **THEN** the prompt includes only safe identity, description, capabilities, triggers, domain, and required input summaries
