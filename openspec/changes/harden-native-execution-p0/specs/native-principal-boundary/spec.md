## ADDED Requirements

### Requirement: Native API derives authorization from a trusted Principal Envelope
The system SHALL construct Native User Context from a host-neutral, versioned Principal Envelope whose subject, tenant, roles, groups, entitlements, and authorization attributes are covered by a valid HMAC signature outside local loopback development. Request body, query, or path identity fields MUST NOT grant identity or permissions.

#### Scenario: Valid complete Principal Envelope
- **WHEN** a non-local Native request supplies a supported Principal Envelope and a valid signature over all claims
- **THEN** the system derives the request User Context exclusively from those verified claims

#### Scenario: Forged body permissions accompany a valid Envelope
- **WHEN** a request body claims roles, groups, entitlements, or authorization attributes that are absent from or conflict with the verified Principal
- **THEN** the body privilege claims are discarded and do not increase access

#### Scenario: Body owner conflicts with the verified Principal
- **WHEN** a request body claims a subject or tenant different from the verified Principal
- **THEN** the system rejects the identity conflict without executing protected work

#### Scenario: Principal signature is missing or invalid outside local development
- **WHEN** a protected Native request outside local loopback access has no valid Principal signature
- **THEN** the system returns `401` and performs no protected read, write, routing, Context retrieval, or Agent invocation

#### Scenario: Legacy owner-only identity is accepted during migration
- **WHEN** a request supplies a valid legacy signature covering only user and tenant
- **THEN** the system derives that owner with empty roles, groups, and entitlements and does not copy permissions from the request body

#### Scenario: Local loopback uses an unsigned Envelope
- **WHEN** `APP_ENV=local` receives a loopback request with a valid unsigned Principal Envelope
- **THEN** the system accepts the Envelope for local development while still refusing body fields as authorization evidence

### Requirement: Native resources enforce Canonical Ownership
The system SHALL scope Native Run, Plan, and Session reads and writes to the authenticated Principal's `(tenant_id, subject)` ownership and SHALL NOT infer tenant-wide or administrator bypass rights.

#### Scenario: Owner reads a Native resource
- **WHEN** a Principal requests its own Run, Plan, or Session messages
- **THEN** the system returns the resource through an owner-scoped query

#### Scenario: Cross-user or cross-tenant resource is requested
- **WHEN** a Principal requests a Run, Plan, or Session owned by another subject or tenant
- **THEN** the system returns `404` without revealing whether the target exists

#### Scenario: Body or query claims a different owner
- **WHEN** a Native resource request includes body or query owner fields different from the authenticated Principal
- **THEN** the system ignores those fields for authorization and does not access the claimed owner's data

#### Scenario: Admin-like role uses a Native owner endpoint
- **WHEN** a Principal with an admin-like role calls a Native owner-scoped endpoint
- **THEN** the role does not bypass Canonical Ownership and any cross-owner operation requires a separate explicit Admin contract

### Requirement: External Native Agent Events require Execution Ticket authority
The system SHALL accept an external Native Agent Event only when an unexpired Execution Ticket with matching purpose authorizes the exact run, turn, owner, agent, and optional plan and step correlation. The system MUST derive those identities from Ticket claims rather than trusting Event payload identity.

#### Scenario: Valid Ticket authorizes progress
- **WHEN** an external executor submits a progress Event with a valid `agent_event` Ticket and matching Event correlation
- **THEN** the system applies the idempotent progress command to the Ticket-bound Delegated Run

#### Scenario: Event omits or forges its Ticket
- **WHEN** an external Native Event has no Ticket or has an invalid, expired, revoked, wrong-purpose, or already-conflicting Ticket
- **THEN** the system rejects the Event without revealing or changing the referenced Run

#### Scenario: Event fields conflict with Ticket claims
- **WHEN** an Event's run, turn, owner, agent, plan, or step differs from the verified Ticket claims
- **THEN** the system rejects the Event and writes no Event, Result, Plan, Turn, or Outbox side effect

#### Scenario: Internal Invoker records execution facts
- **WHEN** a trusted in-process Invoker records an Agent execution fact through the application service
- **THEN** the system may use the internal service boundary without routing the fact through the external Native Event HTTP contract
