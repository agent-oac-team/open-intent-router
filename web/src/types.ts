export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type JsonRecord = Record<string, JsonValue>;

export type RuntimeConfig = {
  app_env: string;
  storage_backend: string;
  registry_backend: string;
  registry_status: string;
  registry_active_source: string;
  registry_message: string;
  registry_agent_count: number;
  route_mode: string;
  router_llm_provider: "mock" | "openai_compatible" | string;
  router_llm_model: string;
  router_llm_base_url: string | null;
  router_prompt_file: string | null;
  router_llm_api_key_configured: boolean;
  admin_api_token_configured: boolean;
  admin_auth_mode: "token_required" | "local_loopback_open" | "token_missing" | string;
  registry_mutation_mode:
    | "read_only_file"
    | "local_dev_write_enabled"
    | "token_required"
    | "disabled_token_missing"
    | string;
  evidence_provider_enabled: boolean;
  evidence_fixed_questions_path: string | null;
  agent_http_timeout_seconds: number;
  memory_enabled: boolean;
  memory_strategy_provider: string;
  memory_prefetch_timeout_seconds: number;
  memory_mem0_collection?: string | null;
  memory_mem0_vector_provider?: string | null;
  memory_mem0_milvus_uri?: string | null;
  memory_mem0_history_backend?: string | null;
  memory_mem0_fail_closed?: boolean;
  memory_mem0_degraded?: boolean;
  memory_mem0_last_error?: string | null;
  memory_mem0_health_status?: string | null;
  memory_formation_mode?: "off" | "observe" | "enforced" | string;
  memory_formation_model_version?: string;
  memory_formation_prompt_version?: string;
  memory_formation_policy_version?: string;
  memory_formation_worker_enabled?: boolean;
  memory_formation_sweeper_enabled?: boolean;
  memory_formation_queue_status?: string;
  knowledge_enabled: boolean;
  knowledge_vector_backend: string;
  knowledge_prefetch_timeout_seconds: number;
  knowledge_milvus_collection?: string | null;
  knowledge_milvus_uri?: string | null;
};

export type ServiceReady = {
  status: string;
  registry_status?: string;
  registry_active_source?: string;
  agent_count?: number;
};

export type TriggerSpec = {
  keywords: string[];
  positive_examples: string[];
  negative_examples: string[];
};

export type AccessPolicy = {
  allow_roles: string[];
  allow_groups: string[];
  allow_tenants: string[];
  deny_roles: string[];
  deny_groups: string[];
  deny_tenants: string[];
  required_attributes: JsonRecord;
};

export type SchemaContract = {
  type: "object";
  required: string[];
  properties: JsonRecord;
};

export type AgentContextSpec = {
  memory?: {
    mode?: "disabled" | "prefetch" | "controlled_retrieval" | string;
    scopes?: string[];
    max_items?: number;
    controlled_retrieval?: JsonRecord | null;
    metadata?: JsonRecord;
  };
  knowledge?: {
    mode?: "disabled" | "prefetch" | "controlled_retrieval" | string;
    source_ids?: string[];
    source_tags?: string[];
    max_items?: number;
    controlled_retrieval?: JsonRecord | null;
    metadata?: JsonRecord;
  };
  metadata?: JsonRecord;
};

export type AgentDefinition = {
  agent_id: string;
  name: string;
  description: string;
  version?: string | null;
  enabled: boolean;
  type: "http" | "local_function" | "mock" | "workflow" | "provider_platform" | "ui_handoff";
  capabilities: string[];
  domain?: string | null;
  tags: string[];
  trigger: TriggerSpec;
  access_policy: AccessPolicy;
  required_inputs: string[];
  optional_inputs: string[];
  input_schema: SchemaContract;
  output_schema: SchemaContract;
  invocation: {
    type: AgentDefinition["type"];
    config: JsonRecord;
    provider_config: JsonRecord;
  };
  ui_handoff: {
    mode: string;
    route?: string | null;
    params: JsonRecord;
  };
  context?: AgentContextSpec;
  priority: number;
  metadata: JsonRecord;
  source: string;
  created_at?: string | null;
  updated_at?: string | null;
};

export type AgentListResponse = {
  agents: AgentDefinition[];
};

export type UserContext = {
  id: string;
  roles: string[];
  groups: string[];
  attributes: JsonRecord;
};

export type RouteRequest = {
  session_id: string;
  source: "host_chat" | "agent_chat" | "agent_event" | "plan_control" | "system";
  user: UserContext;
  input: {
    type: "text";
    text: string;
    attachments: JsonRecord[];
  };
  current_agent?: {
    agent_id: string;
    run_id?: string | null;
    agent_session_id?: string | null;
  } | null;
  event_id?: string | null;
  plan_id?: string | null;
  step_id?: string | null;
  context_budget?: ContextBudgetDebug | null;
  frontend_context: JsonRecord;
};

export type ContextBudgetDebug = {
  max_tokens: number;
  source_budgets: Record<string, number>;
  per_item_token_limit?: number | null;
  per_item_char_limit?: number | null;
  chars_per_token: number;
  allow_summary_placeholder: boolean;
};

export type ContextUsageDebug = {
  budget_tokens: number;
  used_tokens: number;
  usage_source: string;
  included_count: number;
  dropped_count: number;
  truncated_count: number;
  summary_placeholder_count: number;
  source_distribution: Record<string, number>;
  drop_reasons: Record<string, number>;
};

export type ContextSelectionDebug = {
  item_id: string;
  source: string;
  scope: string;
  role?: string | null;
  priority: number;
  relevance: number;
  token_estimate: number;
  char_count: number;
  included: boolean;
  status: "included" | "dropped" | "truncated" | "summary_placeholder" | string;
  drop_reason?: string | null;
  truncated: boolean;
  summary_placeholder: boolean;
  agent_id?: string | null;
  agent_session_id?: string | null;
  created_at?: string | null;
  metadata: JsonRecord;
};

export type ContextPackDebug = {
  pack_id: string;
  request_id: string;
  session_id: string;
  budget: ContextBudgetDebug;
  usage: ContextUsageDebug;
  selection: ContextSelectionDebug[];
  items?: JsonRecord[];
  metadata?: JsonRecord;
  created_at?: string | null;
};

export type RouteResponse = {
  request_id: string;
  session_id: string;
  assistant_message?: string | null;
  decision: {
    status: string;
    action: string;
    target_agent_id?: string | null;
    confidence?: number | null;
    reason: string;
    message: string;
  };
  context: JsonRecord;
  execution_policy?: string | null;
  next_action?: JsonRecord | null;
  plan?: JsonRecord | null;
  invocation?: JsonRecord | null;
  error?: JsonRecord | null;
};

export type InvocationResult = {
  run_id: string;
  agent_id: string;
  status: string;
  message: string;
  output?: JsonRecord | null;
  artifact_refs?: JsonRecord[];
  usage?: JsonRecord;
  error?: JsonRecord | null;
};

export type RouteAndInvokeResponse = {
  route: RouteResponse;
  result?: InvocationResult | null;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  status: "pending" | "completed" | "failed";
  createdAt: string;
  requestId?: string;
};

export type ConversationTurn = {
  id: string;
  mode: "route" | "route-and-invoke";
  requestId?: string;
  userMessage: ChatMessage;
  assistantMessage: ChatMessage;
  routeResponse?: RouteResponse | null;
  invokeResponse?: InvocationResult | null;
  memoryContext?: JsonRecord | null;
  knowledgeContext?: JsonRecord | null;
  agentContext?: JsonRecord | null;
  userId: string;
  tenantId: string;
  memoryTrace: TurnMemoryTraceState;
};

export type TurnMemoryTraceStatus =
  | "idle"
  | "loading"
  | "pending"
  | "success"
  | "not_triggered"
  | "error";

export type TurnMemoryTraceState = {
  status: TurnMemoryTraceStatus;
  data: MemoryDebugResponse | null;
  error?: string;
  pollCount: number;
  updatedAt?: string;
};

export type MemoryDebugFilters = {
  user_id?: string;
  tenant_id?: string;
  agent_id?: string;
  scopes?: string;
  memory_id?: string;
  request_id?: string;
  session_id?: string;
  turn_id?: string;
  run_id?: string;
  formation_job_id?: string;
  memory_key?: string;
  decision_status?: string;
  limit?: string | number;
};

export type MemoryDebugItem = {
  memory_id: string;
  scope: string;
  subject_type?: string;
  subject_id?: string;
  user_id?: string | null;
  tenant_id?: string | null;
  agent_id?: string | null;
  content: string;
  structured_value?: JsonRecord;
  source?: string;
  confidence?: number;
  importance?: number;
  visibility?: string;
  ttl_expires_at?: string | null;
  metadata?: JsonRecord;
  memory_key?: string | null;
  candidate_hash?: string | null;
  current_revision_id?: string | null;
  current_revision_no?: number | null;
  formation_job_id?: string | null;
  lifecycle_status?: string;
  index_status?: string | null;
  canonical_refs?: string[];
  created_at?: string;
  updated_at?: string;
};

export type MemoryDebugEvent = {
  event_id: string;
  event_type: string;
  memory_id?: string | null;
  user_id?: string | null;
  tenant_id?: string | null;
  agent_id?: string | null;
  request_id?: string | null;
  session_id?: string | null;
  turn_id?: string | null;
  run_id?: string | null;
  formation_job_id?: string | null;
  memory_key?: string | null;
  decision_status?: string | null;
  decision_id?: string | null;
  scope?: string | null;
  payload?: JsonRecord;
  created_at?: string;
};

export type MemoryRevisionView = {
  revision_id: string;
  memory_id: string;
  revision_no: number;
  memory_key: string;
  operation: string;
  content_preview?: string | null;
  content_redacted?: boolean;
  confidence: number;
  policy_version: string;
  supersedes_revision_id?: string | null;
  formation_job_id?: string | null;
  created_at: string;
};

export type MemoryTraceLink = {
  request_ids: string[];
  session_id?: string | null;
  turn_ids: string[];
  run_ids: string[];
  memory_ids: string[];
  consumer?: string | null;
  projection_outcome?: string | null;
  relevance?: number | null;
  confidence?: number | null;
};

export type MemoryFormationJobView = {
  job_id: string;
  trigger: string;
  status: string;
  mode: string;
  first_turn_id?: string | null;
  last_turn_id?: string | null;
  source_refs: string[];
  model_version: string;
  prompt_version: string;
  policy_version: string;
  attempt_count: number;
  max_attempts: number;
  last_error_code?: string | null;
  created_at: string;
  updated_at: string;
};

export type MemoryFormationDecisionView = {
  decision_id?: string | null;
  operation_id: string;
  operation: string;
  proposed_operation?: "update" | "delete" | string | null;
  decision_status: string;
  reason_code: string;
  scope?: string | null;
  memory_key: string;
  memory_id?: string | null;
  revision_id?: string | null;
  canonical_refs: string[];
  content_preview?: string | null;
  content_redacted: boolean;
  index_status?: string | null;
  provider_status?: string | null;
};

export type MemoryFormationTraceView = {
  job: MemoryFormationJobView;
  links: MemoryTraceLink;
  scopes: string[];
  decisions: MemoryFormationDecisionView[];
  candidate_count: number;
  decision_counts: Record<string, number>;
  semantic_contract_version?: string | null;
  semantic_validation_counts: Record<string, number>;
  semantic_verifier_counts: Record<string, number>;
  revision_ids: string[];
  model_latency_ms?: number | null;
  provider_latency_ms?: number | null;
  usage: JsonRecord;
};

export type MemoryRequestTrace = {
  request_id: string;
  overall_stage: string;
  terminal: boolean;
  retryable: boolean;
  reason_code?: string | null;
  turn_id?: string | null;
  turn_status?: string | null;
  run_ids: string[];
  result_ids: string[];
  outbox_ids: string[];
  formation_turn_ids: string[];
  formation_job_ids: string[];
  memory_ids: string[];
  revision_ids: string[];
  index_operation_ids: string[];
  updated_at?: string | null;
};

export type MemoryDebugResponse = {
  items: MemoryDebugItem[];
  revisions: MemoryRevisionView[];
  events: MemoryDebugEvent[];
  formation_traces: MemoryFormationTraceView[];
  context_trace_links: MemoryTraceLink[];
  request_trace?: MemoryRequestTrace | null;
  metadata: JsonRecord;
};

export type MemoryIdentity = {
  userId: string;
  tenantId: string;
};

export type MemoryManagementRequest = {
  idempotency_key: string;
  reason: string;
  expected_revision_id?: string | null;
};

export type MemoryManagementOperationResponse = {
  operation_id: string;
  operation: string;
  status: string;
  memory_id?: string | null;
  decision_id?: string | null;
  index_operation_id?: string | null;
  provider_status?: string | null;
  idempotent_replay: boolean;
};

export type KnowledgeDebugFilters = {
  source_ids?: string;
  caller_type?: string;
  caller_id?: string;
  purpose?: string;
  tenant_id?: string;
  limit?: string | number;
};

export type KnowledgeDebugSource = {
  source_id: string;
  name: string;
  description?: string;
  enabled?: boolean;
  allow_roles?: string[];
  allow_groups?: string[];
  allow_tenants?: string[];
  tags?: string[];
  metadata?: JsonRecord;
  created_at?: string;
  updated_at?: string;
};

export type KnowledgeDebugChunk = {
  chunk_id: string;
  source_id: string;
  content: string;
  title?: string | null;
  uri?: string | null;
  tags?: string[];
  metadata?: JsonRecord;
  updated_at?: string;
};

export type KnowledgeDebugLog = {
  log_id: string;
  query: string;
  caller_type: string;
  caller_id?: string | null;
  purpose: string;
  user_id?: string | null;
  tenant_id?: string | null;
  selected_source_ids?: string[];
  denied_source_ids?: string[];
  hit_count?: number;
  status?: string;
  errors?: string[];
  metadata?: JsonRecord;
  created_at?: string;
};

export type KnowledgeDebugResponse = {
  sources: KnowledgeDebugSource[];
  chunks: KnowledgeDebugChunk[];
  logs: KnowledgeDebugLog[];
  metadata: JsonRecord;
};

export type PlanExecutionResponse = {
  plan: JsonRecord;
  results: JsonRecord[];
  next_action?: JsonRecord | null;
};

export type RouteAndExecuteResponse = {
  route: RouteResponse;
  results: JsonRecord[];
  next_action?: JsonRecord | null;
};
