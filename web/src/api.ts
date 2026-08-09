import type {
  AgentDefinition,
  AgentListResponse,
  JsonRecord,
  KnowledgeDebugFilters,
  KnowledgeDebugResponse,
  MemoryDebugFilters,
  MemoryDebugResponse,
  MemoryIdentity,
  MemoryManagementOperationResponse,
  MemoryManagementRequest,
  PlanExecutionResponse,
  RouteAndExecuteResponse,
  RouteAndInvokeResponse,
  RouteRequest,
  RouteResponse,
  RuntimeConfig,
  ServiceReady,
  UserContext,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new ApiError(`Request failed: ${path}`, response.status, data);
  }
  return data as T;
}

function adminHeaders(token: string): HeadersInit {
  return token.trim() ? { "X-Admin-Token": token.trim() } : {};
}

function nativePrincipalHeaders(identity?: MemoryIdentity, user?: UserContext): HeadersInit {
  if (!identity) return {};
  const attributes = Object.fromEntries(
    Object.entries(user?.attributes || {}).filter(([key]) => key !== "tenant" && key !== "tenant_id"),
  );
  const envelope = canonicalJson({
    attributes,
    claims_version: "oir-principal-v1",
    entitlements: normalizeClaims(user?.entitlements || []),
    groups: normalizeClaims(user?.groups || []),
    roles: normalizeClaims(user?.roles || []),
    subject: identity.userId.trim(),
    tenant: identity.tenantId.trim(),
  });
  return {
    "X-OIR-Principal-Envelope": base64UrlUtf8(envelope),
  };
}

function queryString(params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    const normalized = typeof value === "string" ? value.trim() : value;
    if (normalized !== undefined && normalized !== "") {
      query.set(key, String(normalized));
    }
  });
  const serialized = query.toString();
  return serialized ? `?${serialized}` : "";
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  ready: () => request<ServiceReady>("/ready"),
  runtimeConfig: () => request<RuntimeConfig>("/api/v1/runtime/config"),
  listAgents: () => request<AgentListResponse>("/api/v1/agents"),
  adminListAgents: (token: string) =>
    request<AgentListResponse>("/api/v1/admin/agents", { headers: adminHeaders(token) }),
  upsertAgent: (agent: AgentDefinition, token: string) =>
    request<AgentDefinition>("/api/v1/admin/agents", {
      method: "POST",
      headers: adminHeaders(token),
      body: JSON.stringify(agent),
    }),
  updateAgent: (agent: AgentDefinition, token: string) =>
    request<AgentDefinition>(`/api/v1/admin/agents/${encodeURIComponent(agent.agent_id)}`, {
      method: "PUT",
      headers: adminHeaders(token),
      body: JSON.stringify(agent),
    }),
  setAgentEnabled: (agentId: string, enabled: boolean, token: string) =>
    request<AgentDefinition>(`/api/v1/admin/agents/${encodeURIComponent(agentId)}/enabled`, {
      method: "PATCH",
      headers: adminHeaders(token),
      body: JSON.stringify({ enabled }),
    }),
  deleteAgent: (agentId: string, token: string) =>
    request<{ deleted: boolean }>(`/api/v1/admin/agents/${encodeURIComponent(agentId)}`, {
      method: "DELETE",
      headers: adminHeaders(token),
    }),
  route: (payload: RouteRequest) =>
    request<RouteResponse>("/api/v1/route", {
      method: "POST",
      headers: nativePrincipalHeaders(routeIdentity(payload), payload.user),
      body: JSON.stringify(payload),
    }),
  routeAndInvoke: (payload: RouteRequest) =>
    request<RouteAndInvokeResponse>("/api/v1/route-and-invoke", {
      method: "POST",
      headers: nativePrincipalHeaders(routeIdentity(payload), payload.user),
      body: JSON.stringify(payload),
    }),
  routeAndExecute: (payload: RouteRequest) =>
    request<RouteAndExecuteResponse>("/api/v1/route-and-execute", {
      method: "POST",
      headers: nativePrincipalHeaders(routeIdentity(payload), payload.user),
      body: JSON.stringify(payload),
    }),
  memoryDebug: (filters: MemoryDebugFilters = {}, identity?: MemoryIdentity) =>
    request<MemoryDebugResponse>(`/api/v1/memories/debug${queryString(filters)}`, {
      headers: nativePrincipalHeaders(identity),
    }),
  deleteMemory: (memoryId: string, payload: MemoryManagementRequest, identity: MemoryIdentity) =>
    request<MemoryManagementOperationResponse>(
      `/api/v1/memories/${encodeURIComponent(memoryId)}`,
      {
        method: "DELETE",
        headers: nativePrincipalHeaders(identity),
        body: JSON.stringify(payload),
      },
    ),
  resolvePendingMemory: (
    decisionId: string,
    action: "confirm" | "reject",
    payload: MemoryManagementRequest,
    identity: MemoryIdentity,
  ) =>
    request<MemoryManagementOperationResponse>(
      `/api/v1/memories/pending/${encodeURIComponent(decisionId)}/${action}`,
      {
        method: "POST",
        headers: nativePrincipalHeaders(identity),
        body: JSON.stringify(payload),
      },
    ),
  memoryOperation: (operationId: string, identity: MemoryIdentity) =>
    request<MemoryManagementOperationResponse>(
      `/api/v1/memories/operations/${encodeURIComponent(operationId)}`,
      { headers: nativePrincipalHeaders(identity) },
    ),
  knowledgeDebug: (filters: KnowledgeDebugFilters = {}) =>
    request<KnowledgeDebugResponse>(`/api/v1/knowledge/debug${queryString(filters)}`),
  getPlan: (planId: string, identity: MemoryIdentity) =>
    request<JsonRecord>(`/api/v1/plans/${encodeURIComponent(planId)}`, {
      headers: nativePrincipalHeaders(identity),
    }),
  planAction: (planId: string, action: "confirm" | "cancel", identity: MemoryIdentity) =>
    request<JsonRecord>(`/api/v1/plans/${encodeURIComponent(planId)}/actions`, {
      method: "POST",
      headers: nativePrincipalHeaders(identity),
      body: JSON.stringify({
        action,
        user: { id: identity.userId, roles: [], groups: [], attributes: { tenant_id: identity.tenantId } },
      }),
    }),
  executePlan: (planId: string, payload: JsonRecord, identity: MemoryIdentity) =>
    request<PlanExecutionResponse>(`/api/v1/plans/${encodeURIComponent(planId)}/execute`, {
      method: "POST",
      headers: nativePrincipalHeaders(identity, payload.user as unknown as UserContext),
      body: JSON.stringify(payload),
    }),
  confirmAndExecutePlan: (planId: string, payload: JsonRecord, identity: MemoryIdentity) =>
    request<PlanExecutionResponse>(`/api/v1/plans/${encodeURIComponent(planId)}/confirm-and-execute`, {
      method: "POST",
      headers: nativePrincipalHeaders(identity, payload.user as unknown as UserContext),
      body: JSON.stringify(payload),
    }),
  resumePlan: (planId: string, payload: JsonRecord, identity: MemoryIdentity) =>
    request<PlanExecutionResponse>(`/api/v1/plans/${encodeURIComponent(planId)}/resume`, {
      method: "POST",
      headers: nativePrincipalHeaders(identity, payload.user as unknown as UserContext),
      body: JSON.stringify(payload),
    }),
  postAgentEvent: (event: JsonRecord) => {
    const executionTicket = String(event.execution_ticket || "").trim();
    const payload = { ...event };
    delete payload.execution_ticket;
    return request<JsonRecord>("/api/v1/events/agent", {
      method: "POST",
      headers: executionTicket ? { "X-OIR-Execution-Ticket": executionTicket } : undefined,
      body: JSON.stringify(payload),
    });
  },
};

function routeIdentity(payload: RouteRequest): MemoryIdentity | undefined {
  const tenantId = String(payload.user.attributes.tenant_id || payload.user.attributes.tenant || "").trim();
  const userId = payload.user.id.trim();
  return tenantId && userId ? { tenantId, userId } : undefined;
}

function normalizeClaims(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort();
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

function base64UrlUtf8(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}
