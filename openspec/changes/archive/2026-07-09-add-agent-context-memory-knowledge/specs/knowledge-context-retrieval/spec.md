## ADDED Requirements

### Requirement: Knowledge search is a general governed API
The system SHALL provide a general knowledge search API that is governed by caller, purpose, user, tenant, subject, source, and policy metadata rather than being hard-bound to Agent ID.

#### Scenario: Router searches route evidence
- **WHEN** the router calls knowledge search for route evidence
- **THEN** it identifies `caller_type=router` and `purpose=route_evidence`

#### Scenario: Agent execution searches knowledge
- **WHEN** an Agent invocation or fixed workflow node calls knowledge search
- **THEN** it identifies the caller type, optional caller ID, and `purpose=agent_execution`

#### Scenario: Admin previews retrieval
- **WHEN** an admin or debug surface calls knowledge search for inspection
- **THEN** it identifies `purpose=debug` or `purpose=preview` and still passes through policy enforcement

### Requirement: Knowledge source policy is authoritative
The system SHALL enforce knowledge source permissions at the KnowledgeService or source policy layer.

#### Scenario: Agent declares desired sources
- **WHEN** an Agent Definition declares `context.knowledge.source_ids`
- **THEN** those IDs are treated as requested sources, not final proof of access

#### Scenario: Knowledge source denies access
- **WHEN** the current user, tenant, subject, or caller is not allowed to use a requested knowledge source
- **THEN** KnowledgeService excludes that source and records the denial in debug or retrieval logs

#### Scenario: No source remains after policy
- **WHEN** all requested knowledge sources are denied or unavailable
- **THEN** the search returns an empty or degraded result and the Agent invocation continues unless future policy marks knowledge as required

### Requirement: Knowledge context is explicit and structured
The system SHALL assemble `knowledge_context` only when knowledge context is explicitly enabled.

#### Scenario: Knowledge prefetch is disabled
- **WHEN** the target Agent does not enable knowledge prefetch
- **THEN** the router and Invoker do not perform knowledge retrieval for that Agent

#### Scenario: Knowledge prefetch succeeds
- **WHEN** knowledge prefetch is enabled and relevant evidence is retrieved
- **THEN** `knowledge_context.items` includes structured evidence items, `knowledge_context.summary` includes a compact representation, and `knowledge_context.citations` includes source references

#### Scenario: Knowledge prefetch returns no hits
- **WHEN** knowledge prefetch is enabled but no relevant evidence is found
- **THEN** `knowledge_context` is present with empty `items`, empty citations, and `status=empty`

#### Scenario: Knowledge context is truncated
- **WHEN** retrieved knowledge exceeds budget or item limits
- **THEN** lower-value items are removed or summarized and `knowledge_context.truncated` is set to true

### Requirement: Controlled retrieval uses fixed templates
The system SHALL support controlled knowledge retrieval from workflow nodes using predefined templates rather than arbitrary model-selected calls.

#### Scenario: Controlled retrieval node runs
- **WHEN** a workflow node configured for controlled retrieval executes
- **THEN** the system builds the search query from its configured template and allowed variables

#### Scenario: Controlled retrieval references undeclared source
- **WHEN** a controlled retrieval template requests a knowledge source outside its allowed configuration
- **THEN** the system rejects or ignores that source and records the policy outcome

#### Scenario: Controlled retrieval result is passed downstream
- **WHEN** controlled retrieval succeeds
- **THEN** the node output uses the same structured knowledge item and citation schema as `knowledge_context`

### Requirement: Knowledge retrieval uses PostgreSQL and Milvus domains
The system SHALL store knowledge metadata and vector retrieval data in logically separate knowledge domains.

#### Scenario: Knowledge source metadata is stored
- **WHEN** a knowledge source or chunk is registered
- **THEN** its metadata, permissions, source identifiers, update timestamps, and citation fields are stored in PostgreSQL

#### Scenario: Knowledge vectors are indexed
- **WHEN** a knowledge chunk is available for semantic retrieval
- **THEN** its vector representation is stored in a knowledge-specific Milvus collection separate from memory vectors

#### Scenario: Retrieval logs are recorded
- **WHEN** knowledge search is executed
- **THEN** retrieval inputs, selected sources, policy outcomes, hit metadata, failures, and truncation status are recorded for observability

### Requirement: Knowledge retrieval degrades safely
The system SHALL keep normal routing and invocation available when knowledge retrieval fails or times out.

#### Scenario: Knowledge search times out
- **WHEN** knowledge retrieval exceeds its configured timeout
- **THEN** the invocation continues with `knowledge_context.status=timeout` unless future policy marks knowledge as required

#### Scenario: Knowledge provider fails
- **WHEN** a retrieval provider raises an error
- **THEN** the system records the error and returns a degraded knowledge context rather than failing the route by default
