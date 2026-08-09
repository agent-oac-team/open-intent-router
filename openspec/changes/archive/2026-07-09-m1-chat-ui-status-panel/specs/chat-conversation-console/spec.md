## ADDED Requirements

### Requirement: Chat-style local conversation

The visual test UI SHALL render the center conversation area as a chat-style transcript with separate user and assistant messages.

#### Scenario: Empty conversation

- **WHEN** the test UI opens a new session and no message has been sent
- **THEN** the center conversation area shows an empty chat state without rendering numbered turn cards

#### Scenario: Send user message

- **WHEN** a developer submits a user text message
- **THEN** the center conversation area appends a user chat bubble containing the submitted text

#### Scenario: Route response creates assistant message

- **WHEN** the backend route request completes successfully
- **THEN** the center conversation area appends or updates an assistant chat bubble using the current router-visible response text

### Requirement: M1 assistant bubble source

The visual test UI SHALL derive the assistant bubble text from current backend response fields without requiring a backend API change.

#### Scenario: Decision message exists

- **WHEN** a route response contains `decision.message`
- **THEN** the assistant chat bubble displays `decision.message`

#### Scenario: Decision message is empty

- **WHEN** a route response has an empty `decision.message` but contains `decision.reason`
- **THEN** the assistant chat bubble may display `decision.reason` as a compatibility fallback

#### Scenario: Future assistant message compatibility

- **WHEN** a future route response includes top-level `assistant_message`
- **THEN** the assistant text extraction logic can prefer that field without changing the chat transcript rendering contract

### Requirement: Conversation transcript excludes debug details

The center conversation transcript SHALL NOT use the primary chat area to render route actions, target Agent IDs, invocation results, plan steps, or raw JSON debug payloads.

#### Scenario: Route action returned

- **WHEN** a route response contains `decision.action` and `target_agent_id`
- **THEN** those values are visible in the status inspector instead of being embedded in the assistant chat bubble

#### Scenario: Invocation result returned

- **WHEN** route-and-invoke returns an invocation result
- **THEN** the invocation result is visible in the status inspector instead of being rendered as a separate technical block inside the chat transcript

#### Scenario: Plan returned

- **WHEN** a route response contains `plan`
- **THEN** the plan is visible in the status inspector instead of being rendered as a numbered conversation turn card

### Requirement: Conversation controls remain available

The visual test UI SHALL preserve local testing controls needed to build RouteRequest payloads.

#### Scenario: Route mode selection

- **WHEN** a developer selects route-only or route-and-invoke mode before sending a message
- **THEN** the next submitted message uses the selected endpoint behavior

#### Scenario: Advanced context controls

- **WHEN** a developer opens advanced context controls
- **THEN** the UI allows editing source, user context, current Agent context, frontend context, plan id, and step id as supported by the existing request model

#### Scenario: New conversation

- **WHEN** a developer starts a new conversation
- **THEN** the UI clears the chat transcript, latest route state, latest invocation result, plan state, event response, and transient notices
