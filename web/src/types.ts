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
  frontend_context: JsonRecord;
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
