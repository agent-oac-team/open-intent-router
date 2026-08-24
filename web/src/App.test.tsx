import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App, { memoryTracePollDelay, memoryTraceStatus } from "./App";

const mockAgent = {
  agent_id: "script_writer",
  name: "话术生成",
  description: "根据沟通目标生成客户沟通话术。",
  version: "1.0.0",
  revision: 1,
  enabled: true,
  handling_kind: "invocation",
  capabilities: ["话术生成"],
  domain: "ziya_demo",
  tags: ["话术"],
  trigger: {
    keywords: ["话术"],
    positive_examples: ["帮我生成一段客户邀约话术"],
    negative_examples: [],
  },
  access_policy: {
    allow_roles: ["operator"],
    allow_groups: ["default"],
    allow_tenants: ["*"],
    deny_roles: ["suspended"],
    deny_groups: ["restricted"],
    deny_tenants: ["tenant_blocked"],
    any_entitlements: ["advisor:write"],
    required_attributes: { region: "CN" },
  },
  required_inputs: ["text"],
  optional_inputs: [],
  input_schema: {
    type: "object",
    required: ["text"],
    properties: { text: { type: "string" } },
  },
  output_schema: {
    type: "object",
    required: [],
    properties: { draft: { type: "string" } },
  },
  priority: 0,
  source: "database",
};

const mockContextPack = {
  pack_id: "ctx_1",
  request_id: "req_1",
  session_id: "demo_session",
  budget: {
    max_tokens: 80,
    source_budgets: {},
    per_item_token_limit: 12,
    per_item_char_limit: 120,
    chars_per_token: 4,
    allow_summary_placeholder: true,
  },
  usage: {
    budget_tokens: 80,
    used_tokens: 24,
    usage_source: "estimated",
    included_count: 2,
    dropped_count: 1,
    truncated_count: 1,
    summary_placeholder_count: 1,
    source_distribution: { current_input: 1, agent_history: 1 },
    drop_reasons: { total_budget_exceeded: 1 },
  },
  selection: [
    {
      item_id: "current_input",
      source: "current_input",
      scope: "request",
      role: "user",
      priority: 100,
      relevance: 1,
      token_estimate: 4,
      char_count: 16,
      included: true,
      status: "included",
      drop_reason: null,
      truncated: false,
      summary_placeholder: false,
      agent_id: null,
      agent_session_id: null,
      metadata: {},
    },
    {
      item_id: "agent_history:msg_1",
      source: "agent_history",
      scope: "agent",
      role: "agent",
      priority: 48,
      relevance: 0.6,
      token_estimate: 20,
      char_count: 80,
      included: true,
      status: "summary_placeholder",
      drop_reason: "per_item_char_limit",
      truncated: true,
      summary_placeholder: true,
      agent_id: "script_writer",
      agent_session_id: "child_session",
      metadata: {},
    },
    {
      item_id: "host_history:msg_2",
      source: "host_history",
      scope: "session",
      role: "assistant",
      priority: 40,
      relevance: 0.5,
      token_estimate: 90,
      char_count: 360,
      included: false,
      status: "dropped",
      drop_reason: "total_budget_exceeded",
      truncated: false,
      summary_placeholder: false,
      agent_id: null,
      agent_session_id: null,
      metadata: {},
    },
  ],
  metadata: { version: "m4" },
  created_at: "2026-07-02T00:00:00Z",
};

const mockMemoryContext = {
  summary: "用户偏好稳健表达。",
  status: "ok",
  items: [
    {
      memory_id: "mem_pref_language",
      scope: "user_preference",
      content: "用户偏好专业、稳健的表达。",
      relevance: 0.91,
      confidence: 0.86,
      importance: 0.7,
      source: "mem0",
      ttl_expires_at: null,
      metadata: {},
    },
  ],
  truncated: false,
  errors: [],
  metadata: { provider: "mem0" },
};

const mockKnowledgeContext = {
  summary: "风险等级说明来自知识库。",
  status: "ok",
  source_ids: ["risk_guide"],
  items: [
    {
      item_id: "risk_doc_chunk_1",
      source_id: "risk_guide",
      title: "风险等级说明",
      content: "R1 到 R5 表示不同风险等级。",
      score: 0.88,
      uri: "https://example.test/risk",
      metadata: {},
    },
  ],
  citations: [
    {
      source_id: "risk_guide",
      chunk_id: "risk_doc_chunk_1",
      title: "风险等级说明",
      uri: "https://example.test/risk",
      metadata: {},
    },
  ],
  truncated: false,
  errors: [],
  metadata: {
    denied_source_ids: ["private_docs"],
    vector_backend: "milvus",
    collection: "oir_knowledge_vectors",
  },
};

const secondMemoryContext = {
  ...mockMemoryContext,
  summary: "第二轮记忆。",
  items: [
    {
      ...mockMemoryContext.items[0],
      memory_id: "mem_second_turn",
      content: "第二轮使用的记忆。",
    },
  ],
};

const secondKnowledgeContext = {
  ...mockKnowledgeContext,
  summary: "第二轮知识。",
  items: [
    {
      ...mockKnowledgeContext.items[0],
      item_id: "knowledge_second_turn",
      title: "第二轮知识片段",
    },
  ],
  citations: [
    {
      ...mockKnowledgeContext.citations[0],
      chunk_id: "knowledge_second_turn",
      title: "第二轮知识片段",
    },
  ],
};

const mockMemoryDebug = {
  items: [
    {
      memory_id: "mem_debug_1",
      scope: "user_preference",
      subject_type: "user",
      subject_id: "u1",
      user_id: "u1",
      tenant_id: "tenant_a",
      agent_id: "script_writer",
      content: "调试台中的记忆。",
      structured_value: {},
      source: "mem0",
      confidence: 0.82,
      importance: 0.6,
      visibility: "user",
      ttl_expires_at: null,
      metadata: { mem0_memory_id: "m0_1", api_key: "should-not-render" },
      created_at: "2026-07-09T00:00:00Z",
      updated_at: "2026-07-09T00:00:00Z",
    },
  ],
  events: [
    {
      event_id: "evt_1",
      event_type: "memory_added",
      memory_id: "mem_debug_1",
      user_id: "u1",
      tenant_id: "tenant_a",
      agent_id: "script_writer",
      payload: {},
      created_at: "2026-07-09T00:00:00Z",
    },
  ],
  revisions: [
    {
      revision_id: "revision_debug_1",
      memory_id: "mem_debug_1",
      revision_no: 1,
      memory_key: "tenant:tenant_a:user:u1:preference:language",
      operation: "add",
      content_preview: "调试台中的记忆。",
      content_redacted: false,
      confidence: 0.82,
      policy_version: "policy-v1",
      formation_job_id: "job_debug_1",
      created_at: "2026-07-09T00:00:00Z",
    },
  ],
  formation_traces: [
    {
      job: {
        job_id: "job_debug_1",
        trigger: "idle",
        status: "completed",
        mode: "enforced",
        first_turn_id: "turn_1",
        last_turn_id: "turn_5",
        source_refs: ["turn_1", "turn_5"],
        model_version: "formation-v1",
        prompt_version: "prompt-v1",
        policy_version: "policy-v1",
        attempt_count: 1,
        max_attempts: 3,
        last_error_code: null,
        created_at: "2026-07-09T00:00:00Z",
        updated_at: "2026-07-09T00:00:01Z",
      },
      links: {
        request_ids: ["req_debug_1"],
        session_id: "session_debug_1",
        turn_ids: ["turn_1", "turn_5"],
        run_ids: ["run_debug_1"],
        memory_ids: ["mem_debug_1"],
      },
      scopes: ["user_preference"],
      decisions: [
        {
          decision_id: "decision_debug_1",
          operation_id: "operation_debug_1",
          operation: "add",
          decision_status: "accepted",
          reason_code: "accepted_add",
          scope: "user_preference",
          memory_key: "tenant:tenant_a:user:u1:preference:language",
          memory_id: "mem_debug_1",
          revision_id: "revision_debug_1",
          canonical_refs: ["req_debug_1"],
          content_preview: "调试台中的记忆。",
          content_redacted: false,
          index_status: "ready",
          provider_status: "completed",
        },
      ],
      candidate_count: 1,
      decision_counts: { add: 1 },
      semantic_contract_version: "v1",
      semantic_validation_counts: { confirmed: 1 },
      semantic_verifier_counts: { passed: 1 },
      revision_ids: ["revision_debug_1"],
      model_latency_ms: 120,
      provider_latency_ms: 45,
      usage: {},
    },
  ],
  context_trace_links: [],
  metadata: {
    memory_enabled: true,
    strategy_provider: "mem0",
    item_count: 1,
    event_count: 1,
    mem0: {
      status: "ok",
      collection: "oir_memory_vectors",
      api_key: "should-not-render",
    },
  },
};

type TurnTraceScenario =
  | "recall_only"
  | "delayed"
  | "out_of_order"
  | "pending_update"
  | "pending_delete"
  | "pending_add"
  | "pending_missing"
  | "shared_multi_turn"
  | "completed_sensitive"
  | "dead_letter"
  | "formation_retry"
  | "index_pending"
  | "skipped"
  | "trace_missing";

function turnMemoryDebug(
  requestId: string,
  scenario: TurnTraceScenario,
  requestCount: number,
) {
  const second = requestId === "req_2";
  const memoryId = second ? "mem_second_turn" : "mem_pref_language";
  const content = second ? "第二轮使用的记忆。" : "用户偏好专业、稳健的表达。";
  const pending = ["pending_update", "pending_delete", "pending_add", "pending_missing"].includes(
    scenario,
  );
  const pendingOperation =
    scenario === "pending_delete" ? "delete" : scenario === "pending_add" ? "add" : "update";
  const includeFormation =
    pending ||
    scenario === "shared_multi_turn" ||
    scenario === "completed_sensitive" ||
    scenario === "dead_letter" ||
    scenario === "formation_retry" ||
    scenario === "index_pending" ||
    (scenario === "delayed" && requestCount > 1) ||
    (scenario === "out_of_order" && requestCount > 2);
  const sensitive = scenario === "completed_sensitive";
  const deadLetter = scenario === "dead_letter";
  const formationRetry = scenario === "formation_retry";
  const indexPending = scenario === "index_pending";
  const formationTrace = includeFormation
    ? {
        job: {
          job_id: "job_window_1",
          trigger: scenario === "delayed" ? "idle" : "turn_window",
          status: deadLetter ? "dead_letter" : formationRetry ? "retry" : pending ? "pending" : "completed",
          mode: "enforced",
          first_turn_id: "turn_source_1",
          last_turn_id: "turn_source_5",
          source_refs: ["turn_source_1", "turn_source_5"],
          model_version: "formation-v1",
          prompt_version: "prompt-v1",
          policy_version: "policy-v1",
          attempt_count: deadLetter ? 5 : formationRetry ? 2 : 1,
          max_attempts: 5,
          last_error_code: deadLetter || formationRetry ? "provider_timeout" : null,
          created_at: "2026-07-14T00:00:00Z",
          updated_at: "2026-07-14T00:00:01Z",
        },
        links: {
          request_ids: scenario === "shared_multi_turn" ? ["req_1", "req_2"] : [requestId],
          session_id: "demo_session",
          turn_ids: ["turn_source_1", "turn_source_5"],
          run_ids: [second ? "run_2" : "run_1"],
          memory_ids: [memoryId],
          consumer: null,
          projection_outcome: null,
        },
        scopes: ["user_preference"],
        decisions: [
          {
            decision_id: pending ? (scenario === "pending_missing" ? null : `decision_pending_${pendingOperation}`) : "decision_completed",
            operation_id: pending ? `operation_pending_${pendingOperation}` : "operation_completed",
            operation: pending ? "pending" : sensitive ? "reject" : "add",
            proposed_operation: pending && pendingOperation !== "add" ? pendingOperation : null,
            decision_status: pending ? "pending" : sensitive ? "rejected" : "accepted",
            reason_code: pending ? (pendingOperation === "delete" ? "ambiguous_delete" : "confidence_pending") : sensitive ? "sensitive_content" : "accepted_add",
            scope: "user_preference",
            memory_key: "tenant:tenant_a:user:u1:preference:language",
            memory_id: memoryId,
            revision_id: "revision_1",
            canonical_refs: [`canonical_${requestId}`],
            content_preview: sensitive ? "alice@example.com 123-45-6789" : "Use concise answers",
            content_redacted: sensitive,
            index_status: pending ? null : indexPending ? "pending" : "ready",
            provider_status: pending || indexPending ? null : "completed",
          },
        ],
        candidate_count: 1,
        decision_counts: { [pending ? "pending" : sensitive ? "reject" : "add"]: 1 },
        semantic_contract_version: "v1",
        semantic_validation_counts: { [pending ? "pending" : "confirmed"]: 1 },
        semantic_verifier_counts: pending ? { not_configured: 1 } : {},
        revision_ids: pending ? [] : ["revision_1"],
        model_latency_ms: 12,
        provider_latency_ms: 8,
        usage: { input_tokens: 20 },
      }
    : null;
  return {
    items: [
      {
        memory_id: memoryId,
        scope: "user_preference",
        subject_type: "user",
        subject_id: "u1",
        user_id: "u1",
        tenant_id: "tenant_a",
        content,
        source: "mem0",
        confidence: 0.86,
        importance: 0.7,
        visibility: "user",
        memory_key: "tenant:tenant_a:user:u1:preference:language",
        current_revision_id: "revision_1",
        current_revision_no: 1,
        lifecycle_status: "active",
        index_status: indexPending ? "pending" : "ready",
        canonical_refs: [requestId],
        metadata: {},
      },
    ],
    revisions: [],
    events: sensitive
      ? [
          {
            event_id: "event_sensitive",
            event_type: "memory_decision_reject",
            tenant_id: "tenant_a",
            user_id: "u1",
            payload: {
              supporting_quote: "alice@example.com 123-45-6789",
              provider_error: "postgresql://admin:supersecret@localhost/db",
            },
            created_at: "2026-07-14T00:00:01Z",
          },
        ]
      : [],
    formation_traces: formationTrace
      ? scenario === "shared_multi_turn"
        ? [formationTrace, formationTrace]
        : [formationTrace]
      : [],
    request_trace: {
      request_id: requestId,
      overall_stage:
        scenario === "trace_missing"
          ? "trace_missing"
          : scenario === "dead_letter"
            ? "formation_dead_letter"
            : scenario === "formation_retry"
              ? "formation_retry"
              : pending
                ? "formation_pending"
                : scenario === "index_pending"
                  ? "memory_persisted_index_pending"
                  : scenario === "skipped"
                    ? "formation_skipped"
                    : (scenario === "delayed" || scenario === "out_of_order") && !includeFormation
                      ? "outbox_pending"
                      : scenario === "completed_sensitive"
                        ? "policy_rejected"
                        : "persisted",
      terminal: ["dead_letter", "skipped", "completed_sensitive", "recall_only", "shared_multi_turn"].includes(scenario)
        || ((scenario === "delayed" || scenario === "out_of_order") && includeFormation),
      retryable: ["delayed", "out_of_order", "pending_update", "pending_delete", "pending_add", "pending_missing", "formation_retry", "index_pending", "trace_missing"].includes(scenario),
      reason_code:
        scenario === "trace_missing"
          ? "turn_outbox_missing"
          : scenario === "dead_letter" || scenario === "formation_retry"
            ? "provider_timeout"
            : scenario === "skipped"
              ? "formation_mode_off"
              : null,
      turn_id: second ? "turn_2" : "turn_1",
      turn_status: "completed",
      run_ids: [second ? "run_2" : "run_1"],
      result_ids: [second ? "result_2" : "result_1"],
      outbox_ids: [second ? "outbox_2" : "outbox_1"],
      formation_turn_ids: includeFormation ? ["turn_source_1"] : [],
      formation_job_ids: includeFormation ? ["job_window_1"] : [],
      memory_ids: scenario === "trace_missing" || scenario === "skipped" ? [] : [memoryId],
      revision_ids: scenario === "trace_missing" || scenario === "skipped" ? [] : ["revision_1"],
      index_operation_ids: indexPending || scenario === "recall_only" ? ["index_1"] : [],
      updated_at: "2026-07-14T00:00:01Z",
    },
    context_trace_links: [
      {
        request_ids: [requestId],
        session_id: "demo_session",
        turn_ids: [second ? "turn_2" : "turn_1"],
        run_ids: [second ? "run_2" : "run_1"],
        memory_ids: [memoryId],
        consumer: "agent",
        projection_outcome: "included",
        relevance: 0.91,
        confidence: 0.86,
      },
      {
        request_ids: [requestId],
        session_id: "demo_session",
        turn_ids: [second ? "turn_2" : "turn_1"],
        run_ids: [second ? "run_2" : "run_1"],
        memory_ids: ["mem_without_included_outcome"],
        consumer: "router",
        projection_outcome: null,
      },
    ],
    metadata: { item_count: 1, event_count: sensitive ? 1 : 0 },
  };
}

const mockKnowledgeDebug = {
  sources: [
    {
      source_id: "risk_guide",
      name: "风险知识库",
      description: "理财风险等级说明。",
      enabled: true,
      allow_roles: ["operator"],
      allow_groups: [],
      allow_tenants: ["tenant_a"],
      tags: ["risk"],
      metadata: {},
      created_at: "2026-07-09T00:00:00Z",
      updated_at: "2026-07-09T00:00:00Z",
    },
  ],
  chunks: [
    {
      chunk_id: "risk_doc_chunk_1",
      source_id: "risk_guide",
      content: "R1 到 R5 表示不同风险等级。",
      title: "风险等级说明",
      uri: "https://example.test/risk",
      tags: ["risk"],
      metadata: {},
      updated_at: "2026-07-09T00:00:00Z",
    },
  ],
  logs: [
    {
      log_id: "klog_1",
      query: "理财产品风险等级怎么理解",
      caller_type: "agent",
      caller_id: "script_writer",
      purpose: "agent_execution",
      user_id: "u1",
      tenant_id: "tenant_a",
      selected_source_ids: ["risk_guide"],
      denied_source_ids: ["private_docs"],
      hit_count: 1,
      status: "ok",
      errors: [],
      metadata: {},
      created_at: "2026-07-09T00:00:00Z",
    },
  ],
  metadata: {
    knowledge_enabled: true,
    vector_backend: "milvus",
    collection: "oir_knowledge_vectors",
    token: "should-not-render",
  },
};

describe("Memory request trace 状态契约", () => {
  it("以后端终态为准，并在轮询上限后保留 pending", () => {
    expect(memoryTraceStatus(turnMemoryDebug("req_1", "recall_only", 1) as never, "route-and-invoke", 1)).toBe("success");
    expect(memoryTraceStatus(turnMemoryDebug("req_1", "dead_letter", 1) as never, "route-and-invoke", 1)).toBe("error");
    expect(memoryTraceStatus(turnMemoryDebug("req_1", "trace_missing", 1) as never, "route-and-invoke", 1)).toBe("error");
    expect(memoryTraceStatus(turnMemoryDebug("req_1", "formation_retry", 1) as never, "route-and-invoke", 20)).toBe("pending");
    expect(memoryTraceStatus({
      ...mockMemoryDebug,
      items: [],
      events: [],
      formation_traces: [],
      context_trace_links: [],
      request_trace: null,
    }, "route-and-invoke", 20)).toBe("pending");
  });

  it("使用有上限的退避间隔", () => {
    expect(memoryTracePollDelay(0)).toBe(2000);
    expect(memoryTracePollDelay(5)).toBe(4000);
    expect(memoryTracePollDelay(10)).toBe(8000);
    expect(memoryTracePollDelay(15)).toBe(10000);
    expect(memoryTracePollDelay(100)).toBe(10000);
  });
});

describe("意图路由测试台", () => {
  let failMemoryDebug = false;
  let failRouteAndInvoke = false;
  let denyMemoryOperation = false;
  let conflictMemoryOperation = false;
  let memoryProviderTimeout = false;
  let turnTraceScenario: TurnTraceScenario = "recall_only";
  let turnTraceRequests = new Map<string, number>();
  let resolveSlowMemoryTrace: ((response: Response) => void) | null = null;
  let adminAgentPayloads: Record<string, unknown>[] = [];

  beforeEach(() => {
    vi.restoreAllMocks();
    failMemoryDebug = false;
    failRouteAndInvoke = false;
    denyMemoryOperation = false;
    conflictMemoryOperation = false;
    memoryProviderTimeout = false;
    turnTraceScenario = "recall_only";
    turnTraceRequests = new Map();
    resolveSlowMemoryTrace = null;
    adminAgentPayloads = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/health")) return json({ status: "ok" });
        if (url.endsWith("/ready")) return json({ status: "ok", registry_status: "ok" });
        if (url.endsWith("/api/v1/runtime/config")) {
          return json({
            app_env: "local",
            storage_backend: "memory",
            registry_backend: "database",
            registry_status: "ok",
            registry_active_source: "database",
            registry_message: "",
            registry_agent_count: 1,
            route_mode: "route_and_invoke",
            router_llm_provider: "mock",
            router_llm_model: "mock-router",
            router_llm_base_url: null,
            router_prompt_file: "./config/prompts/router.zh.yaml",
            router_llm_api_key_configured: false,
            admin_api_token_configured: false,
            admin_auth_mode: "local_loopback_open",
            registry_mutation_mode: "local_dev_write_enabled",
            evidence_provider_enabled: false,
            evidence_fixed_questions_path: "./config/fixed_questions.example.yaml",
            agent_http_timeout_seconds: 30,
            memory_enabled: true,
            memory_strategy_provider: "mem0",
            memory_prefetch_timeout_seconds: 1,
            memory_mem0_collection: "oir_memory_vectors",
            memory_mem0_vector_provider: "milvus",
            memory_mem0_milvus_uri: ".data/oir_memory_milvus.db",
            memory_mem0_history_backend: "postgresql",
            memory_mem0_fail_closed: true,
            memory_mem0_degraded: false,
            memory_mem0_last_error: null,
            memory_mem0_health_status: "ok",
            knowledge_enabled: true,
            knowledge_vector_backend: "milvus",
            knowledge_prefetch_timeout_seconds: 1,
            knowledge_milvus_collection: "oir_knowledge_vectors",
            knowledge_milvus_uri: ".data/oir_knowledge_milvus.db",
          });
        }
        if (url.endsWith("/api/v1/agents")) return json({ agents: [mockAgent] });
        if (url.endsWith("/api/v1/admin/agents") && (!init?.method || init.method === "GET")) {
          return json({
            agents: [
              {
                ...mockAgent,
                handling: { kind: "invocation", adapter_key: "example_runtime_adapter", config: {} },
              },
            ],
          });
        }
        if (url.endsWith("/api/v1/admin/agents") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body)) as Record<string, unknown>;
          adminAgentPayloads.push(payload);
          return json(payload);
        }
        if (url.includes("/api/v1/admin/agents/") && init?.method === "PUT") {
          const payload = JSON.parse(String(init.body)) as Record<string, unknown>;
          adminAgentPayloads.push(payload);
          return json(payload);
        }
        if (url.includes("/api/v1/memories/debug")) {
          if (failMemoryDebug) return json({ detail: "memory debug down" }, 500);
          const requestId = new URL(url, "http://test.local").searchParams.get("request_id");
          if (requestId) {
            const requestCount = (turnTraceRequests.get(requestId) || 0) + 1;
            turnTraceRequests.set(requestId, requestCount);
            if (turnTraceScenario === "out_of_order" && requestCount === 2) {
              return new Promise<Response>((resolve) => {
                resolveSlowMemoryTrace = resolve;
              });
            }
            return json(turnMemoryDebug(requestId, turnTraceScenario, requestCount));
          }
          return json(mockMemoryDebug);
        }
        if (url.includes("/api/v1/memories/pending/") && init?.method === "POST") {
          if (denyMemoryOperation) return json({ detail: "Memory operation target not found" }, 404);
          const action = url.endsWith("/reject") ? "reject" : "confirm";
          return json({
            operation_id: `operation_${action}`,
            operation: action,
            status: "completed",
            memory_id: "mem_pref_language",
            decision_id: "decision_pending_update",
            index_operation_id: action === "confirm" ? "index_update_1" : null,
            provider_status: action === "confirm" ? "pending" : null,
            idempotent_replay: false,
          });
        }
        if (/\/api\/v1\/memories\/operations\//.test(url)) {
          return json({
            operation_id: "index_update_1",
            operation: "update",
            status: conflictMemoryOperation ? "conflict" : "completed",
            memory_id: "mem_pref_language",
            index_operation_id: "index_update_1",
            provider_status: conflictMemoryOperation ? null : "completed",
            idempotent_replay: false,
          });
        }
        if (/\/api\/v1\/memories\/[^/]+$/.test(url) && init?.method === "DELETE") {
          if (denyMemoryOperation) return json({ detail: "Memory operation target not found" }, 404);
          return json({
            operation_id: "operation_delete",
            operation: "delete",
            status: "pending",
            memory_id: "mem_pref_language",
            index_operation_id: "index_delete_1",
            provider_status: "pending",
            idempotent_replay: false,
          });
        }
        if (url.includes("/api/v1/knowledge/debug")) return json(mockKnowledgeDebug);
        if (url.endsWith("/api/v1/route-and-invoke") && init?.method === "POST") {
          if (failRouteAndInvoke) return json({ detail: "route failed" }, 500);
          const body = JSON.parse(String(init.body));
          const isSecond = String(body.input.text).includes("第二轮");
          return json({
            route: {
              request_id: isSecond ? "req_2" : "req_1",
              session_id: body.session_id,
              assistant_message: isSecond ? "第二轮回答。" : "我会交给话术生成处理。",
              decision: {
                status: "ok",
                action: "open_agent",
                target_agent_id: "script_writer",
                confidence: 0.7,
                reason: "test",
                message: "Routing to 话术生成.",
              },
              context: {
                candidate_agent_ids: ["script_writer"],
                evidence: [],
                metadata: { context_pack: mockContextPack },
              },
              invocation: {
                mode: "deferred",
                agent_id: "script_writer",
                input: {
                  text: body.input.text,
                  memory_context: isSecond
                    ? secondMemoryContext
                    : memoryProviderTimeout
                      ? { ...mockMemoryContext, status: "degraded", errors: ["provider_timeout"] }
                      : mockMemoryContext,
                  knowledge_context: isSecond ? secondKnowledgeContext : mockKnowledgeContext,
                },
              },
            },
            result: {
              run_id: isSecond ? "run_2" : "run_1",
              agent_id: "script_writer",
              status: "completed",
              message: "ok",
              output: { draft: "ok" },
              artifact_refs: [],
              usage: {},
            },
          });
        }
        if (url.endsWith("/api/v1/route") && init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          return json({
            request_id: "req_plan",
            session_id: body.session_id,
            assistant_message: "已生成计划，请在右侧确认。",
            decision: {
              status: "ok",
              action: "reply",
              target_agent_id: null,
              confidence: 0.8,
              reason: "multi intent",
              message: "已生成计划。",
            },
            context: {
              relation: "multi_task",
              candidate_agent_ids: ["script_writer"],
              evidence: [],
              metadata: { context_pack: mockContextPack },
            },
            execution_policy: "require_confirmation",
            next_action: {
              type: "confirm_plan",
              message: "请确认是否执行该计划。",
              plan_id: "plan_1",
            },
            plan: {
              plan_id: "plan_1",
              session_id: body.session_id,
              status: "pending",
              current_step_id: "step_1",
              execution_policy: "require_confirmation",
              next_action: {
                type: "confirm_plan",
                message: "请确认是否执行该计划。",
                plan_id: "plan_1",
              },
              steps: [
                {
                  step_id: "step_1",
                  agent_id: "script_writer",
                  description: "生成话术",
                  status: "pending",
                  depends_on: [],
                  artifact_refs: [],
                },
              ],
            },
          });
        }
        return json({});
      }),
    );
  });

  it("发送消息后显示运行模式和路由结果", async () => {
    render(<App />);

    expect(await screen.findByText("意图路由测试台")).toBeInTheDocument();
    expect(await screen.findByText("mock-router")).toBeInTheDocument();
    const demoPrompts = screen.getByLabelText("演示问题");
    expect(within(demoPrompts).getByRole("button", { name: /话术生成/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /发送/i }));

    const transcript = screen.getByLabelText("聊天记录");
    expect(within(transcript).getByText("帮我生成一段客户邀约话术，语气专业一点。")).toBeInTheDocument();
    expect(await within(transcript).findByText("我会交给话术生成处理。")).toBeInTheDocument();
    expect(await within(transcript).findByText("Recall Used 1")).toBeInTheDocument();
    expect(within(transcript).getByText("Memory considered 1")).toBeInTheDocument();
    expect(within(transcript).getByText("Knowledge 1 / ok")).toBeInTheDocument();
    expect(within(transcript).getByText("Citations 1")).toBeInTheDocument();
    expect(within(transcript).getByText("Denied 1")).toBeInTheDocument();
    expect(within(transcript).queryByText("Routing to 话术生成.")).not.toBeInTheDocument();
    expect(screen.queryByText("第 1 轮")).not.toBeInTheDocument();
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Route/i }));
    await waitFor(() => expect(screen.getAllByText("open_agent").length).toBeGreaterThan(0));
    expect(screen.getAllByText("script_writer").length).toBeGreaterThan(0);
  });

  it.each([
    ["invocation", { kind: "invocation", adapter_key: "example_runtime_adapter", config: {} }],
    ["external_execution", { kind: "external_execution", executor_ref: "oac-executor", params: {} }],
    ["ui_handoff", { kind: "ui_handoff", route: "/host/demo", params: {} }],
  ] as const)("以 Native handling 创建 %s Agent", async (kind, expectedHandling) => {
    render(<App />);

    await screen.findByText("mock-router");
    await userEvent.click(screen.getByRole("button", { name: "新增 Agent" }));
    await userEvent.selectOptions(screen.getByLabelText("Handling"), kind);
    if (kind === "invocation") {
      await userEvent.type(
        screen.getByLabelText("已注册 Runtime Adapter Key"),
        "example_runtime_adapter",
      );
    }
    if (kind === "external_execution") {
      await userEvent.type(screen.getByLabelText("Executor Ref"), "oac-executor");
    }
    if (kind === "ui_handoff") {
      await userEvent.type(screen.getByLabelText("内部 UI 路由"), "/host/demo");
    }
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(adminAgentPayloads).toHaveLength(1));
    expect(adminAgentPayloads[0]).toMatchObject({
      schema_version: "oir-agent-v2",
      handling: expectedHandling,
    });
    expect(adminAgentPayloads[0]).not.toHaveProperty("type");
    expect(adminAgentPayloads[0]).not.toHaveProperty("invocation");
    expect(adminAgentPayloads[0]).not.toHaveProperty("ui_handoff");
  });

  it("编辑 Agent 时保留完整访问策略", async () => {
    render(<App />);

    await screen.findByText("mock-router");
    const agentPanel = screen.getByText("意图与 Agent").closest("section");
    expect(agentPanel).not.toBeNull();
    const agentRow = within(agentPanel!).getByText("话术生成").closest("button");
    expect(agentRow).not.toBeNull();
    await userEvent.click(agentRow!);
    await screen.findByRole("dialog", { name: "Agent 配置" });

    expect(screen.getByLabelText("拒绝角色")).toHaveValue("suspended");
    expect(screen.getByLabelText("拒绝分组")).toHaveValue("restricted");
    expect(screen.getByLabelText("拒绝租户")).toHaveValue("tenant_blocked");
    expect(screen.getByLabelText("任一所需 Entitlement")).toHaveValue("advisor:write");
    expect(screen.getByLabelText("所需属性 JSON")).toHaveValue('{\n  "region": "CN"\n}');

    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(adminAgentPayloads).toHaveLength(1));
    expect(adminAgentPayloads[0]).toMatchObject({
      access_policy: {
        allow_roles: ["operator"],
        allow_groups: ["default"],
        allow_tenants: ["*"],
        deny_roles: ["suspended"],
        deny_groups: ["restricted"],
        deny_tenants: ["tenant_blocked"],
        any_entitlements: ["advisor:write"],
        required_attributes: { region: "CN" },
      },
    });
  });

  it("默认展示中控运行图并保留技术详情标签", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    const statusTabs = screen.getByLabelText("中控状态");
    expect(within(statusTabs).getByRole("tab", { name: "运行图" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("等待一次真实请求")).toBeInTheDocument();
    for (const name of ["Route", "Plan", "Context", "Memory", "Knowledge", "Evidence", "Debug"]) {
      expect(within(statusTabs).getByRole("tab", { name: new RegExp(name, "i") })).toBeInTheDocument();
    }

    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    const journey = await screen.findByLabelText("本轮中控运行链路");
    expect(within(journey).getByText("准备参考信息")).toBeInTheDocument();
    expect(within(journey).getByText("历史记忆 1")).toBeInTheDocument();
    expect(within(journey).getByText("知识资料 1")).toBeInTheDocument();
    expect(within(journey).getByText("话术生成")).toBeInTheDocument();

    await userEvent.click(within(journey).getByRole("button", { name: "查看交给业务助手详情" }));
    const dialog = screen.getByRole("dialog", { name: "交给业务助手" });
    expect(within(dialog).getByText("script_writer")).toBeInTheDocument();
    expect(within(dialog).getByText("话术生成")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "交给业务助手" })).not.toBeInTheDocument();
    expect(within(journey).getByRole("button", { name: "查看交给业务助手详情" })).toHaveFocus();
  });

  it.each([
    ["桌面", 1440],
    ["窄屏", 390],
  ])("%s视口可展开和收起真实 Memory Formation 子流程", async (_label, width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    window.dispatchEvent(new Event("resize"));
    turnTraceScenario = "pending_update";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    const journey = await screen.findByLabelText("本轮中控运行链路");
    const expand = await within(journey).findByRole("button", { name: "展开形成过程" });
    expect(expand).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(expand);
    const subflow = within(journey).getByRole("list", { name: "记忆形成子流程" });
    expect(within(subflow).getAllByRole("listitem")).toHaveLength(7);
    expect(within(subflow).getByText("等待人工处理")).toBeInTheDocument();
    expect(within(subflow).getByText("更新长期记忆")).toBeInTheDocument();
    expect(within(subflow).getByText("更新检索索引")).toBeInTheDocument();
    expect(within(subflow).getAllByText("待处理").length).toBeGreaterThan(0);
    expect(expand).toHaveAttribute("aria-expanded", "true");

    await userEvent.click(expand);
    expect(within(journey).queryByRole("list", { name: "记忆形成子流程" })).not.toBeInTheDocument();
    expect(expand).toHaveFocus();
  });

  it("选择历史轮次时重新投影整张运行图", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("我会交给话术生成处理。")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("用户消息"), "第二轮问题");
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("第二轮回答。")).toBeInTheDocument();
    let journey = screen.getByLabelText("本轮中控运行链路");
    expect(within(journey).getByText("第二轮回答。")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "选择第 1 轮对话" }));
    journey = screen.getByLabelText("本轮中控运行链路");
    expect(within(journey).getByText("我会交给话术生成处理。")).toBeInTheDocument();
    expect(within(journey).queryByText("第二轮回答。")).not.toBeInTheDocument();
  });

  it("agent_chat 缺少当前 Agent 时阻止提交", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByText("高级上下文"));
    await userEvent.selectOptions(screen.getByLabelText("消息来源"), "agent_chat");
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));

    expect(screen.getByText("agent_chat 需要填写当前 Agent ID")).toBeInTheDocument();
  });

  it("有 plan 时不依赖 show_plan 也展示计划和下一步动作", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "只路由" }));
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));

    const transcript = screen.getByLabelText("聊天记录");
    expect(await within(transcript).findByText("已生成计划，请在右侧确认。")).toBeInTheDocument();
    expect(within(transcript).getByText("Context unavailable")).toBeInTheDocument();
    expect(within(transcript).queryByText("请确认是否执行该计划。")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Plan/i }));
    expect(await screen.findByText("plan_1")).toBeInTheDocument();
    expect(screen.getByText(/策略：require_confirmation/)).toBeInTheDocument();
    expect(screen.getByText(/下一步：confirm_plan/)).toBeInTheDocument();
  });

  it("右侧状态面板用 Debug 标签承载调用结果和原始响应", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));

    await userEvent.click(await screen.findByRole("tab", { name: /Debug/i }));
    expect(screen.getByText("调用结果")).toBeInTheDocument();
    expect(screen.getByText("完整路由响应")).toBeInTheDocument();
    expect(screen.queryByText("调用")).not.toBeInTheDocument();
  });

  it("Context 标签展示预算、来源分组和裁剪原因", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(await screen.findByRole("tab", { name: /Context/i }));

    expect(await screen.findByText("24/80 tokens")).toBeInTheDocument();
    expect(screen.getByText("agent_history")).toBeInTheDocument();
    expect(screen.getByText("host_history")).toBeInTheDocument();
    expect(screen.getByText("total_budget_exceeded: 1")).toBeInTheDocument();
    expect(screen.getByText("summary_placeholder")).toBeInTheDocument();
  });

  it("Memory 和 Knowledge 标签按选中轮次分开展示", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("我会交给话术生成处理。")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("用户消息"), "第二轮问题");
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("第二轮回答。")).toBeInTheDocument();

    const statusTabs = screen.getByLabelText("中控状态");
    await userEvent.click(within(statusTabs).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(within(inspector).getByText("mem_second_turn")).toBeInTheDocument();
    expect(within(inspector).queryByText("第二轮知识片段")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "选择第 1 轮对话" }));
    expect(await within(inspector).findByText("mem_pref_language")).toBeInTheDocument();
    expect(within(inspector).getByText(/relevance 0\.91/)).toBeInTheDocument();
    expect(within(inspector).getAllByText(/confidence 0\.86/).length).toBeGreaterThan(0);

    await userEvent.click(within(statusTabs).getByRole("tab", { name: /Knowledge/i }));
    expect(within(inspector).getByText("风险等级说明")).toBeInTheDocument();
    expect(within(inspector).getByText("private_docs")).toBeInTheDocument();
    expect(within(inspector).queryByText("用户偏好专业、稳健的表达。")).not.toBeInTheDocument();
  });

  it("失败轮次不复用上一轮 memory/knowledge trace", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("我会交给话术生成处理。")).toBeInTheDocument();

    failRouteAndInvoke = true;
    await userEvent.type(screen.getByLabelText("用户消息"), "触发失败");
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("请求失败，请查看页面提示或右侧调试信息。")).toBeInTheDocument();

    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(within(inspector).getByText("本轮没有实际进入 Router / Agent 的记忆")).toBeInTheDocument();
    expect(within(inspector).getByText("本轮请求失败，未生成可关联的 Memory Trace。")).toBeInTheDocument();
    expect(within(inspector).queryByText("mem_pref_language")).not.toBeInTheDocument();
  });

  it("只读 debug 管理视图展示 memory 和 knowledge 数据并脱敏", async () => {
    render(<App />);

    expect(await screen.findByText("记忆与知识库调试管理")).toBeInTheDocument();
    expect((await screen.findAllByText("mem_debug_1")).length).toBeGreaterThan(0);
    expect(screen.getByText("运行配置详情").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("高级筛选").closest("details")).not.toHaveAttribute("open");

    const memoryItem = screen.getByRole("button", { name: "查看 Memory Item mem_debug_1" });
    expect(within(memoryItem).queryByText("tenant_a")).not.toBeInTheDocument();
    expect(within(memoryItem).queryByText(/confidence/i)).not.toBeInTheDocument();
    await userEvent.click(memoryItem);

    const detail = screen.getByRole("dialog", { name: "Memory Item 详情" });
    expect(within(detail).getByText("tenant_a")).toBeInTheDocument();
    expect(within(detail).getByText("0.82")).toBeInTheDocument();
    expect(within(detail).getAllByText("调试台中的记忆。").length).toBeGreaterThan(0);
    expect(within(detail).getByText("版本记录")).toBeInTheDocument();
    expect(within(detail).getByText("revision_debug_1")).toBeInTheDocument();
    await userEvent.click(within(detail).getByRole("button", { name: "关闭 Memory Item 详情" }));

    const memoryDataTabs = screen.getByLabelText("Memory 数据视图");
    expect(screen.queryByRole("heading", { name: "Memory Revisions" })).not.toBeInTheDocument();
    await userEvent.click(within(memoryDataTabs).getByRole("tab", { name: /Formation/i }));
    const formationJob = screen.getByRole("button", { name: "查看 Formation Job job_debug_1" });
    expect(within(formationJob).getByText("turn_1 → turn_5")).toBeInTheDocument();
    await userEvent.click(formationJob);

    const formationDetail = screen.getByRole("dialog", { name: "Formation Job 详情" });
    expect(within(formationDetail).getByLabelText("Formation 执行流程")).toBeInTheDocument();
    expect(within(formationDetail).getByText("accepted_add")).toBeInTheDocument();
    expect(within(formationDetail).getAllByText("revision_debug_1").length).toBeGreaterThan(0);
    await userEvent.click(within(formationDetail).getByRole("button", { name: "关闭 Formation Job 详情" }));

    await userEvent.click(within(memoryDataTabs).getByRole("tab", { name: /Events/i }));
    const memoryEvent = screen.getByRole("button", { name: "查看 Memory Event evt_1" });
    expect(within(memoryEvent).getByText("memory_added")).toBeInTheDocument();
    expect(within(memoryEvent).queryByText("tenant_a")).not.toBeInTheDocument();
    expect(within(memoryEvent).queryByText("2026-07-09T00:00:00Z")).not.toBeInTheDocument();
    await userEvent.click(memoryEvent);

    const eventDetail = screen.getByRole("dialog", { name: "Memory Event 详情" });
    expect(within(eventDetail).getByText("tenant_a")).toBeInTheDocument();
    expect(within(eventDetail).getByText("2026-07-09T00:00:00Z")).toBeInTheDocument();
    await userEvent.click(within(eventDetail).getByRole("button", { name: "关闭 Memory Event 详情" }));

    expect(screen.queryByText("should-not-render")).not.toBeInTheDocument();

    const debugTabs = screen.getByLabelText("调试管理视图");
    await userEvent.click(within(debugTabs).getByRole("tab", { name: /Knowledge/i }));
    expect(await screen.findByText("风险知识库")).toBeInTheDocument();
    expect(screen.getAllByText("risk_doc_chunk_1").length).toBeGreaterThan(0);
    expect(screen.getByText("klog_1")).toBeInTheDocument();
    expect(screen.getByText(/canonical 数据/)).toBeInTheDocument();
    expect(screen.getByText(/Milvus collection 只表示向量索引/)).toBeInTheDocument();
    expect(screen.queryByText("should-not-render")).not.toBeInTheDocument();
  });

  it("debug API 失败时显示错误且保留选中轮次 trace", async () => {
    failMemoryDebug = true;
    render(<App />);

    expect(await screen.findByText(/500/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    expect(await within(screen.getByLabelText("聊天记录")).findByText("我会交给话术生成处理。")).toBeInTheDocument();

    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(within(inspector).getByText("mem_pref_language")).toBeInTheDocument();
  });

  it("异步 idle formation 可在原回复后自动轮询并挂回本轮", async () => {
    turnTraceScenario = "delayed";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByText("等待五轮窗口或空闲形成")).toBeInTheDocument();

    expect(
      await within(inspector).findByText("job_window_1", {}, { timeout: 3500 }),
    ).toBeInTheDocument();
    expect(within(inspector).getByText(/idle · enforced/)).toBeInTheDocument();
    expect(within(inspector).getByText(/v1 · validation confirmed 1 · verifier -/)).toBeInTheDocument();
    expect(within(screen.getByLabelText("聊天记录")).getByText("ADD 1")).toBeInTheDocument();
  });

  it("较早的慢轮询响应不会覆盖较新的 completed trace", async () => {
    turnTraceScenario = "out_of_order";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    const refresh = await within(inspector).findByRole("button", { name: "刷新本轮 Memory Trace" });

    await userEvent.click(refresh);
    await userEvent.click(refresh);
    expect(await within(inspector).findByText("job_window_1")).toBeInTheDocument();
    expect(resolveSlowMemoryTrace).not.toBeNull();
    resolveSlowMemoryTrace!(await json(turnMemoryDebug("req_1", "recall_only", 1)));

    await waitFor(() => expect(within(inspector).getByText("job_window_1")).toBeInTheDocument());
    expect(within(inspector).getAllByText("记忆已就绪").length).toBeGreaterThan(0);
  });

  it("五轮 job 展示完整 source range 并提供 pending 更新控制", async () => {
    turnTraceScenario = "pending_update";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByText("turn_source_1 → turn_source_5")).toBeInTheDocument();
    expect(within(inspector).getByText("confidence_pending")).toBeInTheDocument();
    expect(within(inspector).getByText("turn_source_1 · turn_source_5")).toBeInTheDocument();
    expect(within(inspector).getByText("run_1")).toBeInTheDocument();
    expect(within(inspector).getByText("canonical_req_1")).toBeInTheDocument();
    expect(within(inspector).getByText("PENDING 1")).toBeInTheDocument();
    expect(within(screen.getByLabelText("聊天记录")).getByText("PENDING 1")).toBeInTheDocument();
    expect(within(screen.getByLabelText("聊天记录")).queryByText("UPDATE 1")).not.toBeInTheDocument();

    await userEvent.click(within(inspector).getByRole("button", { name: "确认" }));
    expect(await within(inspector).findByText("confirm")).toBeInTheDocument();
    expect(within(inspector).getByText("completed / pending")).toBeInTheDocument();
    const confirmCall = vi.mocked(fetch).mock.calls.find(([input]) =>
      String(input).endsWith("/api/v1/memories/pending/decision_pending_update/confirm"),
    );
    expect(JSON.parse(String(confirmCall?.[1]?.body))).toEqual({
      idempotency_key: "console:confirm:decision_pending_update",
      reason: "confirmed_in_conversation_console",
      expected_revision_id: "revision_1",
    });
    const principalHeaders = new Headers(confirmCall?.[1]?.headers);
    expect(principalHeaders.get("X-OIR-Principal-Envelope")).toBeTruthy();
    expect(principalHeaders.get("X-User-ID")).toBeNull();
    expect(principalHeaders.get("X-Tenant-ID")).toBeNull();
    await userEvent.click(within(inspector).getByRole("button", { name: "刷新 Memory Operation 状态" }));
    expect(await within(inspector).findByText("completed / completed")).toBeInTheDocument();
  });

  it("pending DELETE 保留确认、拒绝和破坏性二次确认", async () => {
    turnTraceScenario = "pending_delete";
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByRole("button", { name: "确认" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "拒绝" })).toBeInTheDocument();

    await userEvent.click(within(inspector).getByRole("button", { name: "确认" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(
      vi.mocked(fetch).mock.calls.some(([input]) =>
        String(input).endsWith("/api/v1/memories/pending/decision_pending_delete/confirm"),
      ),
    ).toBe(true);
  });

  it("pending ADD 只允许拒绝，并在完成后刷新选中 Turn Trace", async () => {
    turnTraceScenario = "pending_add";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByRole("button", { name: "拒绝" })).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: "确认" })).not.toBeInTheDocument();
    const requestCount = turnTraceRequests.get("req_1") || 0;

    await userEvent.click(within(inspector).getByRole("button", { name: "拒绝" }));
    await waitFor(() => expect(turnTraceRequests.get("req_1") || 0).toBeGreaterThan(requestCount));
    expect(
      vi.mocked(fetch).mock.calls.some(([input]) =>
        String(input).endsWith("/api/v1/memories/pending/decision_pending_add/reject"),
      ),
    ).toBe(true);
  });

  it("pending 缺少 decision_id 时显示关联诊断和刷新入口", async () => {
    turnTraceScenario = "pending_missing";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByText(/决策关联数据不完整/)).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: "确认" })).not.toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: "拒绝" })).not.toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "重新加载决策关联" })).toBeInTheDocument();
  });

  it("Recall provider_timeout 不隐藏 Formation pending 操作", async () => {
    turnTraceScenario = "pending_update";
    memoryProviderTimeout = true;
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByText("provider_timeout")).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "确认" })).toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "拒绝" })).toBeInTheDocument();
  });

  it("operation precondition 冲突显示 conflict 状态", async () => {
    turnTraceScenario = "pending_update";
    conflictMemoryOperation = true;
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    await userEvent.click(await within(inspector).findByRole("button", { name: "确认" }));
    await userEvent.click(await within(inspector).findByRole("button", { name: "刷新 Memory Operation 状态" }));

    const feedback = await within(inspector).findByRole("status");
    expect(within(feedback).getByText("conflict / canonical")).toBeInTheDocument();
    expect(feedback).toHaveClass("conflict");
  });

  it("同一 multi-turn job 在参与轮次中共享且不重复 decision", async () => {
    turnTraceScenario = "shared_multi_turn";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.type(screen.getByLabelText("用户消息"), "第二轮问题");
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;

    expect(await within(inspector).findAllByText("job_window_1")).toHaveLength(1);
    expect(within(inspector).getAllByText("accepted_add")).toHaveLength(1);
    expect(within(screen.getByLabelText("聊天记录")).getAllByText("ADD 1")).toHaveLength(2);

    await userEvent.click(screen.getByRole("button", { name: "选择第 1 轮对话" }));
    expect(within(inspector).getAllByText("job_window_1")).toHaveLength(1);
    expect(within(inspector).getAllByText("accepted_add")).toHaveLength(1);
  });

  it("pending 操作被拒绝时只显示有界 denial", async () => {
    turnTraceScenario = "pending_update";
    denyMemoryOperation = true;
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    await userEvent.click(await within(inspector).findByRole("button", { name: "拒绝" }));
    expect(await within(inspector).findByText(/404.*Memory operation target not found/)).toBeInTheDocument();
  });

  it("删除实际使用的记忆需要破坏性确认并显示 pending", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    await userEvent.click(await within(inspector).findByRole("button", { name: "删除记忆 mem_pref_language" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(await within(inspector).findByText("delete")).toBeInTheDocument();
    expect(within(inspector).getByText("pending / pending")).toBeInTheDocument();
    const deleteCall = vi.mocked(fetch).mock.calls.find(
      ([input, init]) => String(input).endsWith("/api/v1/memories/mem_pref_language") && init?.method === "DELETE",
    );
    expect(JSON.parse(String(deleteCall?.[1]?.body))).toEqual({
      idempotency_key: "console:delete:mem_pref_language",
      reason: "deleted_in_conversation_console",
      expected_revision_id: "revision_1",
    });
  });

  it("敏感 rejection 与 raw trace 都不会展示原值", async () => {
    turnTraceScenario = "completed_sensitive";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByText("sensitive_content")).toBeInTheDocument();
    expect(within(inspector).getByText("[redacted]")).toBeInTheDocument();
    expect(within(inspector).queryByText(/alice@example\.com/)).not.toBeInTheDocument();
    expect(within(inspector).queryByText(/123-45-6789/)).not.toBeInTheDocument();
    expect(within(inspector).queryByText(/supersecret/)).not.toBeInTheDocument();
  });

  it.each([
    ["recall_only", "记忆已就绪", "链路已到达终态"],
    ["trace_missing", "链路缺失", "turn_outbox_missing"],
    ["formation_retry", "Formation 重试中", "provider_timeout"],
    ["index_pending", "等待索引", "链路仍在推进"],
    ["skipped", "Formation 已跳过", "formation_mode_off"],
  ] as const)("持续展示 %s 的后端链路状态", async (scenario, label, reason) => {
    turnTraceScenario = scenario;
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findAllByText(label)).not.toHaveLength(0);
    expect(within(inspector).getAllByText(reason).length).toBeGreaterThan(0);
    expect(within(inspector).getByRole("button", { name: "刷新本轮 Memory Trace" })).toBeInTheDocument();
  });

  it("dead-letter formation 显示安全错误且不标记写入成功", async () => {
    turnTraceScenario = "dead_letter";
    render(<App />);

    await waitFor(() => expect(screen.getByText("mock-router")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /发送/i }));
    await userEvent.click(within(screen.getByLabelText("中控状态")).getByRole("tab", { name: /Memory/i }));
    const inspector = screen.getByText("状态面板").closest("section") as HTMLElement;
    expect(await within(inspector).findByText("dead_letter")).toBeInTheDocument();
    expect(within(inspector).getAllByText("provider_timeout").length).toBeGreaterThan(0);
    expect(within(inspector).queryByText("已完成")).not.toBeInTheDocument();
  });
});

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
