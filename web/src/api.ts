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

function memoryIdentityHeaders(identity?: MemoryIdentity): HeadersInit {
  if (!identity) return {};
  return {
    "X-User-ID": identity.userId,
    "X-Tenant-ID": identity.tenantId,
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
      headers: memoryIdentityHeaders(routeIdentity(payload)),
      body: JSON.stringify(payload),
    }),
  routeAndInvoke: (payload: RouteRequest) =>
    request<RouteAndInvokeResponse>("/api/v1/route-and-invoke", {
      method: "POST",
      headers: memoryIdentityHeaders(routeIdentity(payload)),
      body: JSON.stringify(payload),
    }),
  routeAndExecute: (payload: RouteRequest) =>
    request<RouteAndExecuteResponse>("/api/v1/route-and-execute", {
      method: "POST",
      headers: memoryIdentityHeaders(routeIdentity(payload)),
      body: JSON.stringify(payload),
    }),
  memoryDebug: (filters: MemoryDebugFilters = {}, identity?: MemoryIdentity) =>
    request<MemoryDebugResponse>(`/api/v1/memories/debug${queryString(filters)}`, {
      headers: memoryIdentityHeaders(identity),
    }),
  deleteMemory: (memoryId: string, payload: MemoryManagementRequest, identity: MemoryIdentity) =>
    request<MemoryManagementOperationResponse>(
      `/api/v1/memories/${encodeURIComponent(memoryId)}`,
      {
        method: "DELETE",
        headers: memoryIdentityHeaders(identity),
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
        headers: memoryIdentityHeaders(identity),
        body: JSON.stringify(payload),
      },
    ),
  memoryOperation: (operationId: string, identity: MemoryIdentity) =>
    request<MemoryManagementOperationResponse>(
      `/api/v1/memories/operations/${encodeURIComponent(operationId)}`,
      { headers: memoryIdentityHeaders(identity) },
    ),
  knowledgeDebug: (filters: KnowledgeDebugFilters = {}) =>
    request<KnowledgeDebugResponse>(`/api/v1/knowledge/debug${queryString(filters)}`),
  getPlan: (planId: string, identity: MemoryIdentity) =>
    request<JsonRecord>(`/api/v1/plans/${encodeURIComponent(planId)}`, {
      headers: memoryIdentityHeaders(identity),
    }),
  planAction: (planId: string, action: "confirm" | "cancel", identity: MemoryIdentity) =>
    request<JsonRecord>(`/api/v1/plans/${encodeURIComponent(planId)}/actions`, {
      method: "POST",
      headers: memoryIdentityHeaders(identity),
      body: JSON.stringify({
        action,
        user: { id: identity.userId, roles: [], groups: [], attributes: { tenant_id: identity.tenantId } },
      }),
    }),
  executePlan: (planId: string, payload: JsonRecord, identity: MemoryIdentity) =>
    request<PlanExecutionResponse>(`/api/v1/plans/${encodeURIComponent(planId)}/execute`, {
      method: "POST",
      headers: memoryIdentityHeaders(identity),
      body: JSON.stringify(payload),
    }),
  confirmAndExecutePlan: (planId: string, payload: JsonRecord, identity: MemoryIdentity) =>
    request<PlanExecutionResponse>(`/api/v1/plans/${encodeURIComponent(planId)}/confirm-and-execute`, {
      method: "POST",
      headers: memoryIdentityHeaders(identity),
      body: JSON.stringify(payload),
    }),
  resumePlan: (planId: string, payload: JsonRecord, identity: MemoryIdentity) =>
    request<PlanExecutionResponse>(`/api/v1/plans/${encodeURIComponent(planId)}/resume`, {
      method: "POST",
      headers: memoryIdentityHeaders(identity),
      body: JSON.stringify(payload),
    }),
  postAgentEvent: (event: JsonRecord) =>
    request<JsonRecord>("/api/v1/events/agent", {
      method: "POST",
      body: JSON.stringify(event),
    }),
};

function routeIdentity(payload: RouteRequest): MemoryIdentity | undefined {
  const tenantId = String(payload.user.attributes.tenant_id || payload.user.attributes.tenant || "").trim();
  const userId = payload.user.id.trim();
  return tenantId && userId ? { tenantId, userId } : undefined;
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}
