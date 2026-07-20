import {
  Activity,
  AlertTriangle,
  BookOpen,
  Bot,
  Braces,
  CheckCircle2,
  CircleDot,
  ClipboardList,
  Database,
  Eye,
  FileJson,
  GitBranch,
  Loader2,
  MessageSquareText,
  Play,
  Plus,
  RefreshCcw,
  Route,
  Save,
  Send,
  Server,
  Settings2,
  Shield,
  Trash2,
  XCircle,
  Zap,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { api, isApiError } from "./api";
import { projectRoutingJourney } from "./journey";
import type { JourneyNode, JourneyNodeId, JourneyNodeState } from "./journey";
import type {
  AgentDefinition,
  AgentListResponse,
  ChatMessage,
  ConversationTurn,
  ContextPackDebug,
  ContextSelectionDebug,
  KnowledgeDebugFilters,
  KnowledgeDebugResponse,
  MemoryDebugFilters,
  MemoryDebugEvent,
  MemoryDebugItem,
  MemoryDebugResponse,
  MemoryFormationDecisionView,
  MemoryFormationTraceView,
  MemoryManagementOperationResponse,
  MemoryTraceLink,
  JsonRecord,
  JsonValue,
  RouteAndInvokeResponse,
  RouteRequest,
  RouteResponse,
  RuntimeConfig,
  ServiceReady,
} from "./types";

const blankAgent = (): AgentDefinition => ({
  agent_id: "demo_agent",
  name: "演示 Agent",
  description: "描述这个 Agent 能处理的用户意图和可调用能力。",
  version: "0.1.0",
  enabled: true,
  type: "mock",
  capabilities: ["演示能力"],
  domain: "ziya_demo",
  tags: ["demo"],
  trigger: {
    keywords: ["演示"],
    positive_examples: ["运行一个演示请求"],
    negative_examples: [],
  },
  access_policy: {
    allow_roles: ["operator"],
    allow_groups: [],
    allow_tenants: ["*"],
    deny_roles: [],
    deny_groups: [],
    deny_tenants: [],
    required_attributes: {},
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
    properties: {},
  },
  invocation: {
    type: "mock",
    config: { response: { ok: true, message: "演示调用完成" } },
    provider_config: {},
  },
  ui_handoff: {
    mode: "none",
    route: null,
    params: {},
  },
  context: {
    memory: { mode: "disabled", scopes: [], max_items: 5, metadata: {} },
    knowledge: { mode: "disabled", source_ids: [], source_tags: [], max_items: 5, metadata: {} },
    metadata: {},
  },
  priority: 0,
  metadata: {},
  source: "database",
});

type ExecutionMode = "route" | "route-and-invoke";
type LlmModeChoice = "mock" | "openai_compatible";
type DemoPrompt = {
  title: string;
  scenario: string;
  text: string;
  mode: ExecutionMode;
};
type StatusTab = "journey" | "route" | "plan" | "context" | "memory" | "knowledge" | "evidence" | "debug";

const demoPrompts: DemoPrompt[] = [
  {
    title: "话术生成",
    scenario: "单意图",
    text: "帮我生成一段客户邀约话术，语气专业一点。",
    mode: "route-and-invoke",
  },
  {
    title: "访前准备",
    scenario: "单意图",
    text: "明天要拜访一位关注稳健理财的客户，帮我做一下访前准备。",
    mode: "route-and-invoke",
  },
  {
    title: "系统指引",
    scenario: "Evidence 强命中",
    text: "帮我打开客户画像页面",
    mode: "route-and-invoke",
  },
  {
    title: "多步骤计划",
    scenario: "多意图",
    text: "先做访前准备，再生成一段客户沟通话术。",
    mode: "route",
  },
  {
    title: "理财知识",
    scenario: "知识问答",
    text: "理财产品风险等级怎么理解",
    mode: "route-and-invoke",
  },
];

type FormState = {
  agent_id: string;
  name: string;
  description: string;
  type: AgentDefinition["type"];
  enabled: boolean;
  domain: string;
  version: string;
  priority: string;
  capabilities: string;
  tags: string;
  trigger_keywords: string;
  trigger_positive: string;
  trigger_negative: string;
  allow_roles: string;
  allow_groups: string;
  allow_tenants: string;
  required_inputs: string;
  optional_inputs: string;
  input_schema: string;
  output_schema: string;
  invocation_config: string;
  provider_config: string;
  ui_mode: string;
  ui_route: string;
  ui_params: string;
  context: string;
  metadata: string;
};

function App() {
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [ready, setReady] = useState<ServiceReady | null>(null);
  const [health, setHealth] = useState<string>("unknown");
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [agentEditorOpen, setAgentEditorOpen] = useState(false);
  const [form, setForm] = useState<FormState>(agentToForm(blankAgent()));
  const [adminToken, setAdminToken] = useState("");
  const [message, setMessage] = useState(demoPrompts[0].text);
  const [sessionId, setSessionId] = useState("demo_session");
  const [source, setSource] = useState<RouteRequest["source"]>("host_chat");
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("route-and-invoke");
  const [llmModeChoice, setLlmModeChoice] = useState<LlmModeChoice>("mock");
  const [userId, setUserId] = useState("u1");
  const [roles, setRoles] = useState("operator");
  const [groups, setGroups] = useState("default");
  const [tenantId, setTenantId] = useState("tenant_a");
  const [userAttributes, setUserAttributes] = useState('{"tenant_id":"tenant_a"}');
  const [currentAgentId, setCurrentAgentId] = useState("");
  const [currentRunId, setCurrentRunId] = useState("");
  const [agentSessionId, setAgentSessionId] = useState("");
  const [frontendContext, setFrontendContext] = useState("{}");
  const [planId, setPlanId] = useState("");
  const [stepId, setStepId] = useState("");
  const [eventJson, setEventJson] = useState(defaultEventJson());
  const [routeResponse, setRouteResponse] = useState<RouteResponse | null>(null);
  const [invokeResponse, setInvokeResponse] = useState<RouteAndInvokeResponse["result"] | null>(null);
  const [conversationTurns, setConversationTurns] = useState<ConversationTurn[]>([]);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const memoryTraceRequestVersions = useRef(new Map<string, number>());
  const [plan, setPlan] = useState<JsonRecord | null>(null);
  const [eventResponse, setEventResponse] = useState<JsonRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  function selectDemoPrompt(prompt: DemoPrompt) {
    setMessage(prompt.text);
    setExecutionMode(prompt.mode);
  }

  const selectedAgent = useMemo(
    () => agents.find((agent) => agent.agent_id === selectedAgentId) || null,
    [agents, selectedAgentId],
  );

  const registryReadOnly = runtime?.registry_mutation_mode === "read_only_file";
  const registryMutationDisabled = runtime?.registry_mutation_mode === "disabled_token_missing";
  const adminTokenRequired = runtime?.registry_mutation_mode === "token_required";
  const modeMismatch = runtime ? runtime.router_llm_provider !== llmModeChoice : false;
  const canSubmit = source !== "agent_chat" || currentAgentId.trim().length > 0;
  const latestTurn = useMemo(
    () => conversationTurns[conversationTurns.length - 1] || null,
    [conversationTurns],
  );
  const selectedTurn = useMemo(
    () => conversationTurns.find((turn) => turn.id === selectedTurnId) || null,
    [conversationTurns, selectedTurnId],
  );
  const inspectedTurn = selectedTurn || latestTurn;

  const pendingMemoryPolls = useMemo(
    () =>
      conversationTurns.filter(
        (turn) =>
          turn.requestId &&
          turn.memoryTrace.status === "pending" &&
          turn.memoryTrace.pollCount < 20,
      ),
    [conversationTurns],
  );

  useEffect(() => {
    void bootstrap();
  }, []);

  useEffect(() => {
    if (runtime?.router_llm_provider === "openai_compatible") {
      setLlmModeChoice("openai_compatible");
    } else if (runtime?.router_llm_provider === "mock") {
      setLlmModeChoice("mock");
    }
  }, [runtime?.router_llm_provider]);

  useEffect(() => {
    if (selectedAgent) {
      setForm(agentToForm(selectedAgent));
    }
  }, [selectedAgent]);

  useEffect(() => {
    if (!pendingMemoryPolls.length) return undefined;
    const timer = window.setTimeout(() => {
      pendingMemoryPolls.forEach((turn) => void refreshTurnMemoryTrace(turn));
    }, Math.min(...pendingMemoryPolls.map((turn) => memoryTracePollDelay(turn.memoryTrace.pollCount))));
    return () => window.clearTimeout(timer);
  }, [pendingMemoryPolls]);

  async function bootstrap() {
    setBusy(true);
    setNotice("");
    try {
      const [healthResult, readyResult, runtimeResult, agentResult] = await Promise.allSettled([
        api.health(),
        api.ready(),
        api.runtimeConfig(),
        api.listAgents(),
      ]);
      setHealth(healthResult.status === "fulfilled" ? healthResult.value.status : "down");
      if (readyResult.status === "fulfilled") setReady(readyResult.value);
      if (runtimeResult.status === "fulfilled") setRuntime(runtimeResult.value);
      if (agentResult.status === "fulfilled") {
        applyAgents(agentResult.value);
      }
      if (healthResult.status === "rejected" || runtimeResult.status === "rejected") {
        setNotice("后端不可用或配置状态读取失败。");
      }
    } finally {
      setBusy(false);
    }
  }

  function applyAgents(agentResult: AgentListResponse) {
    setAgents(agentResult.agents);
    if (!selectedAgentId && agentResult.agents[0]) {
      setSelectedAgentId(agentResult.agents[0].agent_id);
      setForm(agentToForm(agentResult.agents[0]));
    }
  }

  async function refreshAgents() {
    setNotice("");
    try {
      const response = adminToken ? await api.adminListAgents(adminToken) : await api.listAgents();
      applyAgents(response);
    } catch (error) {
      setNotice(formatError(error));
    }
  }

  function startNewAgent() {
    const agent = blankAgent();
    setSelectedAgentId("");
    setForm(agentToForm(agent));
    setAgentEditorOpen(true);
  }

  function openAgentEditor(agentId: string) {
    setSelectedAgentId(agentId);
    const agent = agents.find((item) => item.agent_id === agentId);
    if (agent) setForm(agentToForm(agent));
    setAgentEditorOpen(true);
  }

  async function saveAgent(event: FormEvent) {
    event.preventDefault();
    if (registryReadOnly) {
      setNotice("当前 Registry 是 file 模式，写操作不可用。");
      return;
    }
    if (registryMutationDisabled) {
      setNotice("非 local 环境必须配置 ADMIN_API_TOKEN 后才能写入 Registry。");
      return;
    }
    try {
      const agent = formToAgent(form);
      const saved = selectedAgent ? await api.updateAgent(agent, adminToken) : await api.upsertAgent(agent, adminToken);
      setNotice(`已保存 ${saved.agent_id}`);
      setSelectedAgentId(saved.agent_id);
      await refreshAgents();
    } catch (error) {
      setNotice(formatError(error));
    }
  }

  async function toggleAgent(agent: AgentDefinition) {
    if (registryReadOnly) {
      setNotice("当前 Registry 是 file 模式，写操作不可用。");
      return;
    }
    if (registryMutationDisabled) {
      setNotice("非 local 环境必须配置 ADMIN_API_TOKEN 后才能写入 Registry。");
      return;
    }
    try {
      await api.setAgentEnabled(agent.agent_id, !agent.enabled, adminToken);
      await refreshAgents();
    } catch (error) {
      setNotice(formatError(error));
    }
  }

  async function deleteAgent(agentId: string) {
    if (registryReadOnly) {
      setNotice("当前 Registry 是 file 模式，写操作不可用。");
      return;
    }
    if (registryMutationDisabled) {
      setNotice("非 local 环境必须配置 ADMIN_API_TOKEN 后才能写入 Registry。");
      return;
    }
    try {
      await api.deleteAgent(agentId, adminToken);
      setSelectedAgentId("");
      setForm(agentToForm(blankAgent()));
      setAgentEditorOpen(false);
      await refreshAgents();
    } catch (error) {
      setNotice(formatError(error));
    }
  }

  function buildRouteRequest(text: string = message): RouteRequest {
    const attributes = parseJsonRecord(userAttributes, "用户属性");
    if (tenantId.trim()) {
      attributes.tenant_id = tenantId.trim();
    }
    const payload: RouteRequest = {
      session_id: sessionId,
      source,
      user: {
        id: userId,
        roles: splitList(roles),
        groups: splitList(groups),
        attributes,
      },
      input: {
        type: "text",
        text,
        attachments: [],
      },
      frontend_context: parseJsonRecord(frontendContext, "前端上下文"),
    };
    if (source === "agent_chat" || currentAgentId.trim()) {
      payload.current_agent = {
        agent_id: currentAgentId.trim(),
        run_id: currentRunId.trim() || null,
        agent_session_id: agentSessionId.trim() || null,
      };
    }
    if (planId.trim()) payload.plan_id = planId.trim();
    if (stepId.trim()) payload.step_id = stepId.trim();
    return payload;
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    setNotice("");
    setPlan(null);
    setEventResponse(null);
    const text = message.trim();
    if (!text) {
      setNotice("请输入一条用户消息。");
      return;
    }
    if (!canSubmit) {
      setNotice("agent_chat 需要填写当前 Agent ID。");
      return;
    }
    const timestamp = Date.now();
    const turnId = `turn_${timestamp}`;
    const userMessageId = `msg_user_${timestamp}`;
    const assistantMessageId = `msg_assistant_${timestamp}`;
    const createdAt = new Date().toISOString();
    const turnUserId = userId.trim();
    const turnTenantId = tenantId.trim();
    const pendingTurn: ConversationTurn = {
      id: turnId,
      mode: executionMode,
      userMessage: { id: userMessageId, role: "user", content: text, status: "completed", createdAt },
      assistantMessage: {
        id: assistantMessageId,
        role: "assistant",
        content: "正在判断意图...",
        status: "pending",
        createdAt,
      },
      routeResponse: null,
      invokeResponse: null,
      memoryContext: null,
      knowledgeContext: null,
      agentContext: null,
      userId: turnUserId,
      tenantId: turnTenantId,
      memoryTrace: { status: "loading", data: null, pollCount: 0 },
    };
    setConversationTurns((current) => [
      ...current,
      pendingTurn,
    ]);
    setSelectedTurnId(turnId);
    setMessage("");
    setBusy(true);
    try {
      const payload = buildRouteRequest(text);
      if (executionMode === "route") {
        const result = await api.route(payload);
        const trace = traceFromRoute(result);
        setRouteResponse(result);
        setInvokeResponse(null);
        if (result.plan && typeof result.plan === "object") setPlan(result.plan as JsonRecord);
        setConversationTurns((current) =>
          current.map((turn) =>
            turn.id === turnId
              ? {
                  ...turn,
                  requestId: result.request_id,
                  routeResponse: result,
                  invokeResponse: null,
                  memoryContext: trace.memoryContext,
                  knowledgeContext: trace.knowledgeContext,
                  agentContext: trace.agentContext,
                  assistantMessage: {
                    ...turn.assistantMessage,
                    content: assistantTextFromRoute(result),
                    status: "completed",
                    requestId: result.request_id,
                  },
                }
              : turn,
          ),
        );
        setSelectedTurnId(turnId);
        void loadTurnMemoryTrace({
          turnId,
          requestId: result.request_id,
          mode: executionMode,
          userId: turnUserId,
          tenantId: turnTenantId,
          pollCount: 0,
        });
      } else {
        const result = await api.routeAndInvoke(payload);
        const trace = traceFromRoute(result.route);
        setRouteResponse(result.route);
        setInvokeResponse(result.result || null);
        if (result.route.plan && typeof result.route.plan === "object") setPlan(result.route.plan as JsonRecord);
        setConversationTurns((current) =>
          current.map((turn) =>
            turn.id === turnId
              ? {
                  ...turn,
                  requestId: result.route.request_id,
                  routeResponse: result.route,
                  invokeResponse: result.result || null,
                  memoryContext: trace.memoryContext,
                  knowledgeContext: trace.knowledgeContext,
                  agentContext: trace.agentContext,
                  assistantMessage: {
                    ...turn.assistantMessage,
                    content: assistantTextFromRoute(result.route),
                    status: "completed",
                    requestId: result.route.request_id,
                  },
                }
              : turn,
          ),
        );
        setSelectedTurnId(turnId);
        void loadTurnMemoryTrace({
          turnId,
          requestId: result.route.request_id,
          mode: executionMode,
          userId: turnUserId,
          tenantId: turnTenantId,
          pollCount: 0,
        });
      }
    } catch (error) {
      setNotice(formatError(error));
      setRouteResponse(null);
      setInvokeResponse(null);
      setConversationTurns((current) =>
        current.map((turn) =>
          turn.id === turnId
            ? {
                ...turn,
                requestId: undefined,
                routeResponse: null,
                invokeResponse: null,
                memoryContext: null,
                knowledgeContext: null,
                agentContext: null,
                memoryTrace: {
                  status: "error",
                  data: null,
                  error: "本轮请求失败，未生成可关联的 Memory Trace。",
                  pollCount: 0,
                },
                assistantMessage: {
                  ...turn.assistantMessage,
                  content: "请求失败，请查看页面提示或右侧调试信息。",
                  status: "failed",
                },
              }
            : turn,
        ),
      );
      setSelectedTurnId(turnId);
    } finally {
      setBusy(false);
    }
  }

  async function refreshTurnMemoryTrace(turn: ConversationTurn) {
    if (!turn.requestId) return;
    await loadTurnMemoryTrace({
      turnId: turn.id,
      requestId: turn.requestId,
      mode: turn.mode,
      userId: turn.userId,
      tenantId: turn.tenantId,
      pollCount: turn.memoryTrace.pollCount,
    });
  }

  async function loadTurnMemoryTrace(target: {
    turnId: string;
    requestId: string;
    mode: ExecutionMode;
    userId: string;
    tenantId: string;
    pollCount: number;
  }) {
    const requestVersion = (memoryTraceRequestVersions.current.get(target.turnId) || 0) + 1;
    memoryTraceRequestVersions.current.set(target.turnId, requestVersion);
    if (!target.userId || !target.tenantId) {
      setConversationTurns((current) =>
        current.map((turn) =>
          turn.id === target.turnId
            ? {
                ...turn,
                memoryTrace: {
                  status: "error",
                  data: null,
                  error: "Memory Trace 需要可信用户与租户身份。",
                  pollCount: target.pollCount,
                },
              }
            : turn,
        ),
      );
      return;
    }
    try {
      const data = await api.memoryDebug(
        {
          user_id: target.userId,
          tenant_id: target.tenantId,
          request_id: target.requestId,
          limit: 100,
        },
        { userId: target.userId, tenantId: target.tenantId },
      );
      if (memoryTraceRequestVersions.current.get(target.turnId) !== requestVersion) return;
      const nextPollCount = target.pollCount + 1;
      setConversationTurns((current) =>
        current.map((turn) =>
          turn.id === target.turnId && turn.requestId === target.requestId
            ? {
                ...turn,
                memoryTrace: {
                  status: memoryTraceStatus(
                    data,
                    target.mode,
                    nextPollCount,
                    runtime?.memory_formation_mode,
                  ),
                  data,
                  pollCount: nextPollCount,
                  updatedAt: new Date().toISOString(),
                },
              }
            : turn,
        ),
      );
    } catch (error) {
      if (memoryTraceRequestVersions.current.get(target.turnId) !== requestVersion) return;
      setConversationTurns((current) =>
        current.map((turn) =>
          turn.id === target.turnId && turn.requestId === target.requestId
            ? {
                ...turn,
                memoryTrace: {
                  status: "error",
                  data: null,
                  error: formatError(error),
                  pollCount: target.pollCount + 1,
                },
              }
            : turn,
        ),
      );
    }
  }

  function startNewConversation() {
    const nextSessionId = `demo_${Date.now()}`;
    setSessionId(nextSessionId);
    setMessage("");
    setConversationTurns([]);
    memoryTraceRequestVersions.current.clear();
    setSelectedTurnId(null);
    setRouteResponse(null);
    setInvokeResponse(null);
    setPlan(null);
    setEventResponse(null);
    setNotice("");
    setSource("host_chat");
    setCurrentAgentId("");
    setCurrentRunId("");
    setAgentSessionId("");
    setPlanId("");
    setStepId("");
    setFrontendContext("{}");
  }

  async function refreshPlan() {
    const targetPlanId = planId.trim() || String(routeResponse?.plan?.plan_id || "");
    if (!targetPlanId) {
      setNotice("需要 plan_id。");
      return;
    }
    try {
      setPlan(await api.getPlan(targetPlanId, { userId: userId.trim(), tenantId: tenantId.trim() }));
    } catch (error) {
      setNotice(formatError(error));
    }
  }

  function buildPlanExecutionPayload(extraInput: JsonRecord = {}): JsonRecord {
    return {
      user: {
        id: userId,
        roles: splitList(roles),
        groups: splitList(groups),
        attributes: parseJsonRecord(userAttributes, "用户属性"),
      },
      input: {
        text: message.trim() || "plan execution",
        query: message.trim() || "plan execution",
        title: message.trim() || "plan execution",
        ...extraInput,
      },
      context: {
        frontend_context: parseJsonRecord(frontendContext, "前端上下文"),
      },
      max_steps: 10,
    };
  }

  async function runPlanAction(action: "confirm-and-execute" | "execute" | "resume" | "cancel") {
    const targetPlanId = planId.trim() || String(plan?.plan_id || routeResponse?.plan?.plan_id || "");
    if (!targetPlanId) {
      setNotice("需要 plan_id。");
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      if (action === "cancel") {
        const identity = { userId: userId.trim(), tenantId: tenantId.trim() };
        await api.planAction(targetPlanId, "cancel", identity);
        setPlan(await api.getPlan(targetPlanId, identity));
        return;
      }
      const payload = buildPlanExecutionPayload();
      const identity = { userId: userId.trim(), tenantId: tenantId.trim() };
      const response =
        action === "confirm-and-execute"
          ? await api.confirmAndExecutePlan(targetPlanId, payload, identity)
          : action === "resume"
            ? await api.resumePlan(targetPlanId, payload, identity)
            : await api.executePlan(targetPlanId, payload, identity);
      setPlan(response.plan);
      if (response.results[0]) {
        const first = response.results[0];
        const nextInvokeResponse = {
          run_id: String(first.run_id || ""),
          agent_id: String(first.agent_id || ""),
          status: String(first.status || ""),
          message: String(first.message || ""),
          output: (first.output as JsonRecord | null) || null,
          artifact_refs: Array.isArray(first.artifact_refs) ? (first.artifact_refs as JsonRecord[]) : [],
          usage: {},
          error: (first.error as JsonRecord | null) || null,
        };
        setInvokeResponse(nextInvokeResponse);
        const targetTurnId = selectedTurnId || latestTurn?.id || null;
        if (targetTurnId) {
          setConversationTurns((current) =>
            current.map((turn) =>
              turn.id === targetTurnId
                ? {
                    ...turn,
                    invokeResponse: nextInvokeResponse,
                  }
                : turn,
            ),
          );
        }
      }
    } catch (error) {
      setNotice(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  async function submitEvent() {
    try {
      const payload = parseJsonRecord(eventJson, "事件 JSON");
      const response = await api.postAgentEvent(payload);
      setEventResponse(response);
    } catch (error) {
      setNotice(formatError(error));
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Open Intent Router</p>
          <h1>意图路由测试台</h1>
        </div>
        <div className="topbar-actions">
          <StatusPill label="服务" value={health} tone={health === "ok" ? "good" : "bad"} />
          <StatusPill
            label="注册表"
            value={runtime?.registry_status || ready?.registry_status || "unknown"}
            tone={(runtime?.registry_status || ready?.registry_status) === "ok" ? "good" : "warn"}
          />
          <button className="icon-button" type="button" onClick={bootstrap} aria-label="刷新运行状态">
            {busy ? <Loader2 className="spin" size={17} /> : <RefreshCcw size={17} />}
          </button>
        </div>
      </header>

      {notice ? (
        <section className="notice" role="alert">
          <AlertTriangle size={16} />
          <span>{notice}</span>
        </section>
      ) : null}

      <section className="workspace">
        <aside className="left-rail">
          <RuntimePanel runtime={runtime} modeChoice={llmModeChoice} setModeChoice={setLlmModeChoice} />
          {modeMismatch ? (
            <div className="warning-strip">
              <AlertTriangle size={15} />
              <span>当前选择与后端配置不一致，修改 .env 后需重启后端。</span>
            </div>
          ) : null}
          <AgentPanel
            agents={agents}
            selectedAgentId={selectedAgentId}
            onOpenAgent={openAgentEditor}
            onRefresh={refreshAgents}
            onNew={startNewAgent}
            onToggle={toggleAgent}
            runtime={runtime}
          />
        </aside>

        <section className="center-stage">
          <ConversationPanel
            message={message}
            setMessage={setMessage}
            sessionId={sessionId}
            setSessionId={setSessionId}
            source={source}
            setSource={setSource}
            executionMode={executionMode}
            setExecutionMode={setExecutionMode}
            userId={userId}
            setUserId={setUserId}
            roles={roles}
            setRoles={setRoles}
            groups={groups}
            setGroups={setGroups}
            tenantId={tenantId}
            setTenantId={setTenantId}
            userAttributes={userAttributes}
            setUserAttributes={setUserAttributes}
            currentAgentId={currentAgentId}
            setCurrentAgentId={setCurrentAgentId}
            currentRunId={currentRunId}
            setCurrentRunId={setCurrentRunId}
            agentSessionId={agentSessionId}
            setAgentSessionId={setAgentSessionId}
            frontendContext={frontendContext}
            setFrontendContext={setFrontendContext}
            planId={planId}
            setPlanId={setPlanId}
            stepId={stepId}
            setStepId={setStepId}
            canSubmit={canSubmit}
            busy={busy}
            onSubmit={sendMessage}
            onNewConversation={startNewConversation}
            onSelectDemoPrompt={selectDemoPrompt}
            turns={conversationTurns}
            selectedTurnId={inspectedTurn?.id || null}
            onSelectTurn={setSelectedTurnId}
          />
          <DebugManagementPanel runtime={runtime} />
        </section>

        <aside className="right-rail">
          <StatusInspector
            turn={inspectedTurn}
            agents={agents}
            plan={plan}
            planId={planId}
            setPlanId={setPlanId}
            eventJson={eventJson}
            setEventJson={setEventJson}
            eventResponse={eventResponse}
            onRefreshPlan={refreshPlan}
            onPlanAction={runPlanAction}
            onSubmitEvent={submitEvent}
            onRefreshMemoryTrace={refreshTurnMemoryTrace}
          />
        </aside>
      </section>
      {agentEditorOpen ? (
        <AgentEditorModal
          form={form}
          setForm={setForm}
          adminToken={adminToken}
          setAdminToken={setAdminToken}
          onSave={saveAgent}
          onDelete={selectedAgentId ? () => void deleteAgent(selectedAgentId) : undefined}
          onClose={() => setAgentEditorOpen(false)}
          registryReadOnly={registryReadOnly}
          registryMutationDisabled={registryMutationDisabled}
          adminTokenRequired={adminTokenRequired}
          isExisting={Boolean(selectedAgentId)}
        />
      ) : null}
    </main>
  );
}

function RuntimePanel({
  runtime,
  modeChoice,
  setModeChoice,
}: {
  runtime: RuntimeConfig | null;
  modeChoice: LlmModeChoice;
  setModeChoice: (value: LlmModeChoice) => void;
}) {
  return (
    <section className="panel runtime-panel">
      <PanelTitle icon={<Server size={18} />} title="运行状态" />
        <div className="metric-grid">
          <Metric label="模型提供方" value={runtime?.router_llm_provider || "unknown"} />
          <Metric label="模型" value={runtime?.router_llm_model || "-"} />
          <Metric label="注册来源" value={runtime?.registry_active_source || "-"} />
          <Metric label="Agent 数量" value={String(runtime?.registry_agent_count ?? "-")} />
          <Metric label="Memory" value={runtime ? `${runtime.memory_enabled ? "on" : "off"} / ${runtime.memory_strategy_provider}` : "-"} />
          <Metric label="Knowledge" value={runtime ? `${runtime.knowledge_enabled ? "on" : "off"} / ${runtime.knowledge_vector_backend}` : "-"} />
        </div>
      <div className="segmented" role="radiogroup" aria-label="路由模式">
        <button
          type="button"
          className={modeChoice === "mock" ? "active" : ""}
          onClick={() => setModeChoice("mock")}
        >
          <Bot size={15} />
          本地 Mock
        </button>
        <button
          type="button"
          className={modeChoice === "openai_compatible" ? "active" : ""}
          onClick={() => setModeChoice("openai_compatible")}
        >
          <Zap size={15} />
          大模型
        </button>
      </div>
      <dl className="compact-list">
        <div>
          <dt>接口地址</dt>
          <dd>{runtime?.router_llm_base_url || "local"}</dd>
        </div>
        <div>
          <dt>提示词</dt>
          <dd>{runtime?.router_prompt_file || "-"}</dd>
        </div>
        <div>
          <dt>API Key</dt>
          <dd>{runtime?.router_llm_api_key_configured ? "已配置" : "未配置"}</dd>
        </div>
        <div>
          <dt>管理鉴权</dt>
          <dd>{adminModeLabel(runtime)}</dd>
        </div>
        <div>
          <dt>写入模式</dt>
          <dd>{mutationModeLabel(runtime)}</dd>
        </div>
      </dl>
    </section>
  );
}

function AgentPanel({
  agents,
  selectedAgentId,
  onOpenAgent,
  onRefresh,
  onNew,
  onToggle,
  runtime,
}: {
  agents: AgentDefinition[];
  selectedAgentId: string;
  onOpenAgent: (id: string) => void;
  onRefresh: () => void;
  onNew: () => void;
  onToggle: (agent: AgentDefinition) => void;
  runtime: RuntimeConfig | null;
}) {
  const registryReadOnly = runtime?.registry_mutation_mode === "read_only_file";
  const localWriteEnabled = runtime?.registry_mutation_mode === "local_dev_write_enabled";
  const tokenRequired = runtime?.registry_mutation_mode === "token_required";
  const disabledTokenMissing = runtime?.registry_mutation_mode === "disabled_token_missing";
  return (
    <section className="panel agent-list-panel">
      <div className="panel-title-row">
        <PanelTitle icon={<Database size={18} />} title="意图与 Agent" />
        <div className="button-row">
          <button className="icon-button small" type="button" onClick={onRefresh} aria-label="刷新 Agent">
            <RefreshCcw size={15} />
          </button>
          <button className="icon-button small" type="button" onClick={onNew} aria-label="新增 Agent">
            <Plus size={15} />
          </button>
        </div>
      </div>
      {registryReadOnly ? (
        <div className="inline-note">
          <Shield size={14} />
          <span>文件注册表只读</span>
        </div>
      ) : null}
      {localWriteEnabled ? (
        <div className="inline-note good">
          <Shield size={14} />
          <span>本地 loopback 写入已启用，无需 Admin Token</span>
        </div>
      ) : null}
      {tokenRequired ? (
        <div className="inline-note">
          <Shield size={14} />
          <span>写操作需要 Admin Token</span>
        </div>
      ) : null}
      {disabledTokenMissing ? (
        <div className="inline-note danger">
          <Shield size={14} />
          <span>非 local 环境缺少 ADMIN_API_TOKEN，写操作禁用</span>
        </div>
      ) : null}
      <div className="agent-list">
        {agents.map((agent) => (
          <button
            type="button"
            className={`agent-row ${selectedAgentId === agent.agent_id ? "selected" : ""}`}
            key={agent.agent_id}
            onClick={() => onOpenAgent(agent.agent_id)}
          >
            <span className={`agent-dot ${agent.enabled ? "on" : "off"}`} />
            <span className="agent-row-main">
              <strong>{agent.name}</strong>
              <small>{agent.agent_id} · {agent.description}</small>
            </span>
            <span className="agent-type">{agent.type}</span>
            <span
              className="toggle-chip"
              role="switch"
              aria-checked={agent.enabled}
              onClick={(event) => {
                event.stopPropagation();
                onToggle(agent);
              }}
            >
              {agent.enabled ? "启用" : "停用"}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function AgentEditorModal({
  form,
  setForm,
  adminToken,
  setAdminToken,
  onSave,
  onDelete,
  onClose,
  registryReadOnly,
  registryMutationDisabled,
  adminTokenRequired,
  isExisting,
}: {
  form: FormState;
  setForm: (form: FormState) => void;
  adminToken: string;
  setAdminToken: (token: string) => void;
  onSave: (event: FormEvent) => void;
  onDelete?: () => void;
  onClose: () => void;
  registryReadOnly: boolean;
  registryMutationDisabled: boolean;
  adminTokenRequired: boolean;
  isExisting: boolean;
}) {
  const update = (key: keyof FormState, value: string | boolean) => {
    setForm({ ...form, [key]: value });
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="panel editor-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Agent 配置"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="panel-title-row sticky-modal-head">
          <PanelTitle icon={<FileJson size={18} />} title="Agent 配置" />
          <div className="button-row">
            {onDelete ? (
              <button className="icon-button danger" type="button" onClick={onDelete} aria-label="删除 Agent">
                <Trash2 size={16} />
              </button>
            ) : null}
            <button className="icon-button small" type="button" onClick={onClose} aria-label="关闭配置">
              <XCircle size={16} />
            </button>
          </div>
        </div>
        <form className="agent-form" onSubmit={onSave}>
        <div className="form-grid three">
          <TextField
            label={adminTokenRequired ? "Admin Token（必填）" : "Admin Token（可选）"}
            value={adminToken}
            onChange={setAdminToken}
            type="password"
          />
          <TextField label="Agent ID" value={form.agent_id} onChange={(value) => update("agent_id", value)} />
          <TextField label="名称" value={form.name} onChange={(value) => update("name", value)} />
        </div>
        <div className="form-grid three">
          <SelectField
            label="类型"
            value={form.type}
            onChange={(value) => {
              update("type", value);
              update("ui_mode", value === "ui_handoff" ? "host_route" : form.ui_mode);
            }}
            options={["mock", "http", "local_function", "ui_handoff", "workflow", "provider_platform"]}
          />
          <TextField label="领域" value={form.domain} onChange={(value) => update("domain", value)} />
          <TextField label="优先级" value={form.priority} onChange={(value) => update("priority", value)} />
        </div>
        <label className="toggle-line">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(event) => update("enabled", event.target.checked)}
          />
          启用
        </label>
        <TextAreaField
          label="描述"
          value={form.description}
          onChange={(value) => update("description", value)}
          rows={3}
        />
        <div className="form-grid two">
          <TextAreaField label="能力列表" value={form.capabilities} onChange={(value) => update("capabilities", value)} />
          <TextAreaField label="标签" value={form.tags} onChange={(value) => update("tags", value)} />
        </div>
        <div className="form-grid three">
          <TextAreaField label="触发关键词" value={form.trigger_keywords} onChange={(value) => update("trigger_keywords", value)} />
          <TextAreaField label="正向示例" value={form.trigger_positive} onChange={(value) => update("trigger_positive", value)} />
          <TextAreaField label="反向示例" value={form.trigger_negative} onChange={(value) => update("trigger_negative", value)} />
        </div>
        <div className="form-grid three">
          <TextField label="允许角色" value={form.allow_roles} onChange={(value) => update("allow_roles", value)} />
          <TextField label="允许分组" value={form.allow_groups} onChange={(value) => update("allow_groups", value)} />
          <TextField label="允许租户" value={form.allow_tenants} onChange={(value) => update("allow_tenants", value)} />
        </div>
        <div className="form-grid two">
          <TextField label="必填输入" value={form.required_inputs} onChange={(value) => update("required_inputs", value)} />
          <TextField label="可选输入" value={form.optional_inputs} onChange={(value) => update("optional_inputs", value)} />
        </div>
        <div className="form-grid two">
          <TextAreaField label="输入 Schema JSON" value={form.input_schema} onChange={(value) => update("input_schema", value)} rows={6} />
          <TextAreaField label="输出 Schema JSON" value={form.output_schema} onChange={(value) => update("output_schema", value)} rows={6} />
        </div>
        <div className="form-grid three">
          <TextAreaField label="调用配置" value={form.invocation_config} onChange={(value) => update("invocation_config", value)} rows={5} />
          <TextAreaField label="Provider 配置" value={form.provider_config} onChange={(value) => update("provider_config", value)} rows={5} />
          <TextAreaField label="元数据" value={form.metadata} onChange={(value) => update("metadata", value)} rows={5} />
        </div>
        <div className="form-grid three">
          <TextField label="UI 模式" value={form.ui_mode} onChange={(value) => update("ui_mode", value)} />
          <TextField label="UI 路由" value={form.ui_route} onChange={(value) => update("ui_route", value)} />
          <TextAreaField label="UI 参数 JSON" value={form.ui_params} onChange={(value) => update("ui_params", value)} rows={3} />
        </div>
        <TextAreaField label="Context JSON" value={form.context} onChange={(value) => update("context", value)} rows={8} />
        <div className="submit-row">
          <span>{registryReadOnly ? "文件注册表只读" : registryMutationDisabled ? "管理写入已禁用" : isExisting ? "编辑现有 Agent" : "新增 Agent"}</span>
          <button type="submit" className="primary-button" disabled={registryReadOnly || registryMutationDisabled}>
            <Save size={16} />
            保存
          </button>
        </div>
        </form>
      </section>
    </div>
  );
}

function ConversationPanel(props: {
  message: string;
  setMessage: (value: string) => void;
  sessionId: string;
  setSessionId: (value: string) => void;
  source: RouteRequest["source"];
  setSource: (value: RouteRequest["source"]) => void;
  executionMode: ExecutionMode;
  setExecutionMode: (value: ExecutionMode) => void;
  userId: string;
  setUserId: (value: string) => void;
  roles: string;
  setRoles: (value: string) => void;
  groups: string;
  setGroups: (value: string) => void;
  tenantId: string;
  setTenantId: (value: string) => void;
  userAttributes: string;
  setUserAttributes: (value: string) => void;
  currentAgentId: string;
  setCurrentAgentId: (value: string) => void;
  currentRunId: string;
  setCurrentRunId: (value: string) => void;
  agentSessionId: string;
  setAgentSessionId: (value: string) => void;
  frontendContext: string;
  setFrontendContext: (value: string) => void;
  planId: string;
  setPlanId: (value: string) => void;
  stepId: string;
  setStepId: (value: string) => void;
  canSubmit: boolean;
  busy: boolean;
  onSubmit: (event: FormEvent) => void;
  onNewConversation: () => void;
  onSelectDemoPrompt: (prompt: DemoPrompt) => void;
  turns: ConversationTurn[];
  selectedTurnId: string | null;
  onSelectTurn: (turnId: string) => void;
}) {
  return (
    <section className="panel conversation-panel">
      <div className="panel-title-row">
        <PanelTitle icon={<MessageSquareText size={18} />} title="对话测试" />
        <button className="secondary-button compact" type="button" onClick={props.onNewConversation}>
          <Plus size={16} />
          新对话
        </button>
      </div>
      <form onSubmit={props.onSubmit} className="conversation-form">
        <div className="chat-toolbar">
          <div className="segmented compact-segmented" role="radiogroup" aria-label="执行模式">
            <button
              type="button"
              className={props.executionMode === "route" ? "active" : ""}
              onClick={() => props.setExecutionMode("route")}
            >
              <Route size={15} />
              只路由
            </button>
            <button
              type="button"
              className={props.executionMode === "route-and-invoke" ? "active" : ""}
              onClick={() => props.setExecutionMode("route-and-invoke")}
            >
              <Play size={15} />
              路由并调用
            </button>
          </div>
          <TextField label="会话 ID" value={props.sessionId} onChange={props.setSessionId} />
        </div>
        <DemoPromptShelf onSelect={props.onSelectDemoPrompt} />
        <ChatTranscript
          turns={props.turns}
          selectedTurnId={props.selectedTurnId}
          onSelectTurn={props.onSelectTurn}
        />
        <TextAreaField label="用户消息" value={props.message} onChange={props.setMessage} rows={4} />
        <details className="advanced-options">
          <summary>高级上下文</summary>
          <div className="form-grid three">
            <SelectField
              label="消息来源"
              value={props.source}
              onChange={(value) => props.setSource(value as RouteRequest["source"])}
              options={["host_chat", "agent_chat", "agent_event", "plan_control", "system"]}
            />
            <TextField label="用户 ID" value={props.userId} onChange={props.setUserId} />
            <TextField label="租户 ID" value={props.tenantId} onChange={props.setTenantId} />
          </div>
          <div className="form-grid two">
            <TextField label="角色" value={props.roles} onChange={props.setRoles} />
            <TextField label="分组" value={props.groups} onChange={props.setGroups} />
          </div>
          <div className="form-grid two">
            <TextAreaField label="用户属性 JSON" value={props.userAttributes} onChange={props.setUserAttributes} rows={4} />
            <TextAreaField label="前端上下文 JSON" value={props.frontendContext} onChange={props.setFrontendContext} rows={4} />
          </div>
          <div className="form-grid three">
            <TextField label="当前 Agent ID" value={props.currentAgentId} onChange={props.setCurrentAgentId} />
            <TextField label="Run ID" value={props.currentRunId} onChange={props.setCurrentRunId} />
            <TextField label="Agent 会话 ID" value={props.agentSessionId} onChange={props.setAgentSessionId} />
          </div>
          <div className="form-grid two">
            <TextField label="Plan ID" value={props.planId} onChange={props.setPlanId} />
            <TextField label="Step ID" value={props.stepId} onChange={props.setStepId} />
          </div>
        </details>
        {!props.canSubmit ? (
          <div className="inline-error">
            <XCircle size={15} />
            agent_chat 需要填写当前 Agent ID
          </div>
        ) : null}
        <div className="submit-row">
          <span>{props.executionMode === "route" ? "只返回路由结果" : "路由后调用基础 Invoker"}</span>
          <button type="submit" className="primary-button" disabled={!props.canSubmit || props.busy}>
            {props.busy ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
            发送
          </button>
        </div>
      </form>
    </section>
  );
}

function DemoPromptShelf({ onSelect }: { onSelect: (prompt: DemoPrompt) => void }) {
  return (
    <section className="demo-prompt-shelf" aria-label="演示问题">
      <div className="demo-prompt-head">
        <strong>固定演示问题</strong>
        <span>点击填入，不自动发送</span>
      </div>
      <div className="demo-prompt-grid">
        {demoPrompts.map((prompt) => (
          <button
            type="button"
            className="demo-prompt-card"
            key={prompt.title}
            onClick={() => onSelect(prompt)}
          >
            <span>{prompt.scenario}</span>
            <strong>{prompt.title}</strong>
            <small>{prompt.text}</small>
          </button>
        ))}
      </div>
    </section>
  );
}

function ChatTranscript({
  turns,
  selectedTurnId,
  onSelectTurn,
}: {
  turns: ConversationTurn[];
  selectedTurnId: string | null;
  onSelectTurn: (turnId: string) => void;
}) {
  if (!turns.length) {
    return <EmptyState icon={<MessageSquareText size={20} />} label="选择演示问题或直接输入用户请求" />;
  }
  return (
    <div className="chat-transcript" aria-label="聊天记录">
      {turns.map((turn, index) => (
        <button
          type="button"
          className={`chat-turn ${turn.assistantMessage.status} ${selectedTurnId === turn.id ? "selected" : ""}`}
          key={turn.id}
          onClick={() => onSelectTurn(turn.id)}
          aria-pressed={selectedTurnId === turn.id}
          aria-label={`选择第 ${index + 1} 轮对话`}
        >
          <ChatMessageBubble message={turn.userMessage} />
          <ChatMessageBubble message={turn.assistantMessage}>
            <TurnTraceBadges turn={turn} />
          </ChatMessageBubble>
        </button>
      ))}
    </div>
  );
}

function ChatMessageBubble({ message, children }: { message: ChatMessage; children?: React.ReactNode }) {
  return (
    <article className={`chat-message ${message.role} ${message.status}`}>
      <div className="chat-avatar" aria-hidden="true">
        {message.role === "user" ? "U" : message.role === "assistant" ? "AI" : "S"}
      </div>
      <div className="chat-bubble">
        <div className="chat-meta">
          <span>{message.role === "user" ? "用户" : message.role === "assistant" ? "中控" : "系统"}</span>
          <strong>{chatStatusLabel(message.status)}</strong>
        </div>
        <p>{message.content}</p>
        {children}
      </div>
    </article>
  );
}

function TurnTraceBadges({ turn }: { turn: ConversationTurn }) {
  if (turn.assistantMessage.status === "pending") {
    return (
      <div className="turn-trace-badges">
        <span>Context 等待中</span>
      </div>
    );
  }
  const memory = contextTraceSummary(turn.memoryContext ?? null);
  const knowledge = contextTraceSummary(turn.knowledgeContext ?? null);
  const deniedCount = deniedSourceCount(turn.knowledgeContext ?? null);
  const recallUsed = actualRecallRecords(turn.memoryTrace.data).length;
  const formationCounts = formationDecisionCounts(
    uniqueFormationTraces(turn.memoryTrace.data?.formation_traces || []),
  );
  return (
    <div className="turn-trace-badges">
      <span>Recall Used {turn.memoryTrace.status === "loading" ? "…" : recallUsed}</span>
      <span>Memory considered {memory.itemCount}</span>
      {Object.entries(formationCounts).map(([operation, count]) => (
        <span key={operation}>{operation.toUpperCase()} {count}</span>
      ))}
      {turn.memoryTrace.status === "pending" ? <span>Formation pending</span> : null}
      {turn.memoryTrace.status === "error" ? <span>Formation error</span> : null}
      <span>Knowledge {knowledge.label}</span>
      <span>Citations {knowledge.citationCount}</span>
      <span>Denied {deniedCount}</span>
      <span>{contextAvailabilityLabel(turn)}</span>
    </div>
  );
}

function StatusInspector({
  turn,
  agents,
  plan,
  planId,
  setPlanId,
  eventJson,
  setEventJson,
  eventResponse,
  onRefreshPlan,
  onPlanAction,
  onSubmitEvent,
  onRefreshMemoryTrace,
}: {
  turn: ConversationTurn | null;
  agents: AgentDefinition[];
  plan: JsonRecord | null;
  planId: string;
  setPlanId: (value: string) => void;
  eventJson: string;
  setEventJson: (value: string) => void;
  eventResponse: JsonRecord | null;
  onRefreshPlan: () => void;
  onPlanAction: (action: "confirm-and-execute" | "execute" | "resume" | "cancel") => void;
  onSubmitEvent: () => void;
  onRefreshMemoryTrace: (turn: ConversationTurn) => void;
}) {
  const [activeTab, setActiveTab] = useState<StatusTab>("journey");
  const routeResponse = turn?.routeResponse || null;
  const invokeResponse = turn?.invokeResponse || null;
  const turnPlan = routeResponse?.plan && typeof routeResponse.plan === "object" ? (routeResponse.plan as JsonRecord) : null;
  const tabs: Array<{ id: StatusTab; label: string; icon: React.ReactNode }> = [
    { id: "journey", label: "运行图", icon: <GitBranch size={15} /> },
    { id: "route", label: "Route", icon: <Route size={15} /> },
    { id: "plan", label: "Plan", icon: <ClipboardList size={15} /> },
    { id: "context", label: "Context", icon: <Braces size={15} /> },
    { id: "memory", label: "Memory", icon: <Database size={15} /> },
    { id: "knowledge", label: "Knowledge", icon: <BookOpen size={15} /> },
    { id: "evidence", label: "Evidence", icon: <Eye size={15} /> },
    { id: "debug", label: "Debug", icon: <FileJson size={15} /> },
  ];

  return (
    <section className="panel status-inspector">
      <PanelTitle icon={<Eye size={18} />} title="状态面板" />
      <div className="selected-turn-strip">
        <span>当前轮次</span>
        <strong>{turn?.requestId || turn?.id || "未选择"}</strong>
      </div>
      <div className="status-tabs" role="tablist" aria-label="中控状态">
        {tabs.map((tab) => (
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            className={activeTab === tab.id ? "active" : ""}
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </div>
      <div className="status-tab-panel" role="tabpanel">
        {activeTab === "journey" ? <JourneyTab turn={turn} agents={agents} /> : null}
        {activeTab === "route" ? (
          <RouteTab routeResponse={routeResponse} invokeResponse={invokeResponse} />
        ) : null}
        {activeTab === "plan" ? (
          <PlanTab
            plan={turnPlan || plan}
            planId={planId}
            setPlanId={setPlanId}
            onRefreshPlan={onRefreshPlan}
            onPlanAction={onPlanAction}
          />
        ) : null}
        {activeTab === "context" ? <ContextTab routeResponse={routeResponse} /> : null}
        {activeTab === "memory" ? (
          <MemoryTab
            turn={turn}
            routeResponse={routeResponse}
            onRefresh={() => turn && onRefreshMemoryTrace(turn)}
          />
        ) : null}
        {activeTab === "knowledge" ? (
          <KnowledgeTab knowledgeContext={turn?.knowledgeContext || null} routeResponse={routeResponse} />
        ) : null}
        {activeTab === "evidence" ? <EvidenceTab routeResponse={routeResponse} /> : null}
        {activeTab === "debug" ? (
          <DebugTab
            routeResponse={routeResponse}
            invokeResponse={invokeResponse}
            eventJson={eventJson}
            setEventJson={setEventJson}
            eventResponse={eventResponse}
            onSubmitEvent={onSubmitEvent}
          />
        ) : null}
      </div>
    </section>
  );
}

function JourneyTab({ turn, agents }: { turn: ConversationTurn | null; agents: AgentDefinition[] }) {
  const journey = useMemo(() => projectRoutingJourney(turn, agents), [turn, agents]);
  const [selectedNodeId, setSelectedNodeId] = useState<JourneyNodeId | null>(null);
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null);
  const selectedNode = journey.nodes.find((node) => node.id === selectedNodeId) || null;

  useEffect(() => {
    setSelectedNodeId(null);
  }, [turn?.id]);

  function openDetails(node: JourneyNode, trigger: HTMLButtonElement) {
    detailTriggerRef.current = trigger;
    setSelectedNodeId(node.id);
  }

  function closeDetails() {
    setSelectedNodeId(null);
    window.setTimeout(() => detailTriggerRef.current?.focus(), 0);
  }

  if (journey.state === "empty") {
    return (
      <div className="journey-empty">
        <EmptyState icon={<GitBranch size={22} />} label={journey.title} />
        <p>{journey.summary}</p>
      </div>
    );
  }

  return (
    <div className="journey-view">
      <header className={`journey-overview ${journey.state}`} aria-live="polite">
        <span className="journey-overview-mark" aria-hidden="true">
          {journey.state === "processing" ? <Loader2 className="spin" size={18} /> : journey.state === "failed" ? <XCircle size={18} /> : <CheckCircle2 size={18} />}
        </span>
        <div>
          <small>本轮中控链路</small>
          <strong>{journey.title}</strong>
          <p>{journey.summary}</p>
        </div>
      </header>

      <ol className="journey-flow" aria-label="本轮中控运行链路">
        {journey.nodes.map((node, index) => {
          const hasDetails = node.details.length > 0 || Boolean(node.steps?.length);
          return (
            <li className={`journey-stage ${node.state}`} key={node.id}>
              <span className="journey-sequence" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
              <button
                type="button"
                className="journey-node"
                disabled={!hasDetails}
                onClick={(event) => openDetails(node, event.currentTarget)}
                aria-label={hasDetails ? `查看${node.label}详情` : `${node.label}，${journeyStateLabel(node.state)}`}
              >
                <span className="journey-node-icon" aria-hidden="true">{journeyNodeIcon(node.id)}</span>
                <span className="journey-node-copy">
                  <span className="journey-node-head">
                    <strong>{node.label}</strong>
                    <small>{journeyStateLabel(node.state)}</small>
                  </span>
                  <span className="journey-node-summary" title={node.summary}>{node.summary}</span>
                  {node.tags?.length ? (
                    <span className="journey-node-tags">
                      {node.tags.map((tag) => <span key={tag}>{tag}</span>)}
                    </span>
                  ) : null}
                  {node.steps?.length ? (
                    <span className="journey-plan-preview" aria-label="协作步骤摘要">
                      {node.steps.slice(0, 3).map((step) => (
                        <span key={step.id}>
                          <i className={journeyStepTone(step.status)} aria-hidden="true" />
                          <b>{step.label}</b>
                          <small>{step.agentName}</small>
                        </span>
                      ))}
                      {node.steps.length > 3 ? <em>另有 {node.steps.length - 3} 个步骤</em> : null}
                    </span>
                  ) : null}
                </span>
                {hasDetails ? <Eye className="journey-node-open" size={15} aria-hidden="true" /> : null}
              </button>
            </li>
          );
        })}
      </ol>

      {selectedNode ? <JourneyDetailModal node={selectedNode} onClose={closeDetails} /> : null}
    </div>
  );
}

function JourneyDetailModal({ node, onClose }: { node: JourneyNode; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="modal-backdrop journey-detail-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="panel journey-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`journey-detail-${node.id}`}
      >
        <div className="panel-title-row journey-detail-head">
          <div>
            <span className={`journey-detail-state ${node.state}`}>{journeyStateLabel(node.state)}</span>
            <h2 id={`journey-detail-${node.id}`}>{node.label}</h2>
          </div>
          <button ref={closeRef} className="icon-button small" type="button" onClick={onClose} aria-label="关闭运行节点详情">
            <XCircle size={17} />
          </button>
        </div>
        <p className="journey-detail-summary">{node.summary}</p>
        {node.details.length ? (
          <dl className="journey-detail-fields">
            {node.details.map((field) => (
              <div key={`${field.label}-${field.value}`}>
                <dt>{field.label}</dt>
                <dd>{field.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {node.steps?.length ? (
          <div className="journey-detail-steps">
            <h3>协作步骤</h3>
            {node.steps.map((step, index) => (
              <div key={step.id}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <strong>{step.label}</strong>
                <small>{step.agentName}</small>
                <em className={journeyStepTone(step.status)}>{step.status}</em>
              </div>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function journeyNodeIcon(id: JourneyNodeId): React.ReactNode {
  const icons: Record<JourneyNodeId, React.ReactNode> = {
    input: <MessageSquareText size={17} />,
    context: <BookOpen size={17} />,
    routing: <Route size={17} />,
    handoff: <Bot size={17} />,
    invocation: <Zap size={17} />,
    response: <Send size={17} />,
    formation: <Database size={17} />,
  };
  return icons[id];
}

function journeyStateLabel(state: JourneyNodeState): string {
  const labels: Record<JourneyNodeState, string> = {
    waiting: "等待",
    active: "处理中",
    completed: "已完成",
    skipped: "未使用",
    failed: "未完成",
  };
  return labels[state];
}

function journeyStepTone(status: string): string {
  const normalized = status.toLowerCase();
  if (["completed", "success", "succeeded"].includes(normalized)) return "completed";
  if (["failed", "error", "cancelled", "canceled"].includes(normalized)) return "failed";
  if (["running", "active", "in_progress"].includes(normalized)) return "active";
  return "waiting";
}

function RouteTab({
  routeResponse,
  invokeResponse,
}: {
  routeResponse: RouteResponse | null;
  invokeResponse: RouteAndInvokeResponse["result"] | null;
}) {
  const decision = routeResponse?.decision;
  const candidates = arrayContextValue(routeResponse, "candidate_agent_ids");
  if (!decision) {
    return <EmptyState icon={<CircleDot size={20} />} label="等待路由响应" />;
  }
  return (
    <div className="status-section">
      <div className="decision-card">
        <span className="decision-action">{decision.action}</span>
        <strong>{decision.target_agent_id || "无目标 Agent"}</strong>
        <p>{decision.message || decision.reason || "路由完成"}</p>
        <div className="decision-meta">
          <span>状态：{decision.status}</span>
          <span>置信度：{typeof decision.confidence === "number" ? decision.confidence.toFixed(2) : "-"}</span>
        </div>
      </div>
      <dl className="detail-list">
        <div>
          <dt>原因</dt>
          <dd>{decision.reason || "-"}</dd>
        </div>
        <div>
          <dt>消息</dt>
          <dd>{decision.message || "-"}</dd>
        </div>
        <div>
          <dt>候选 Agent</dt>
          <dd>{candidates.length ? candidates.map(String).join(", ") : "-"}</dd>
        </div>
      </dl>
      {routeResponse?.invocation ? <JsonBlock title="调用预览" value={routeResponse.invocation} defaultOpen={false} /> : null}
      {invokeResponse ? (
        <JsonBlock
          title="调用摘要"
          value={{
            run_id: invokeResponse.run_id,
            agent_id: invokeResponse.agent_id,
            status: invokeResponse.status,
            message: invokeResponse.message,
          }}
          defaultOpen={false}
        />
      ) : null}
    </div>
  );
}

function PlanTab({
  plan,
  planId,
  setPlanId,
  onRefreshPlan,
  onPlanAction,
}: {
  plan: JsonRecord | null;
  planId: string;
  setPlanId: (value: string) => void;
  onRefreshPlan: () => void;
  onPlanAction: (action: "confirm-and-execute" | "execute" | "resume" | "cancel") => void;
}) {
  const steps = Array.isArray(plan?.steps) ? plan.steps : [];
  const nextAction = plan?.next_action && typeof plan.next_action === "object" ? (plan.next_action as JsonRecord) : null;
  return (
    <div className="status-section">
      <div className="inline-controls">
        <input value={planId} onChange={(event) => setPlanId(event.target.value)} placeholder="plan_id" />
        <button className="icon-button small" type="button" onClick={onRefreshPlan} aria-label="刷新 Plan">
          <RefreshCcw size={15} />
        </button>
      </div>
      {plan ? (
        <div className="plan-summary">
          <div className="plan-head">
            <strong>{String(plan.plan_id || "plan")}</strong>
            <span>{String(plan.status || "-")}</span>
          </div>
          <div className="decision-meta">
            <span>策略：{String(plan.execution_policy || "-")}</span>
            <span>下一步：{String(nextAction?.type || "-")}</span>
          </div>
          {steps.map((step, index) => (
            <div className="plan-step" key={`${String((step as JsonRecord).step_id || index)}`}>
              <span>{String((step as JsonRecord).status || "pending")}</span>
              <strong>{String((step as JsonRecord).description || (step as JsonRecord).step_id || index)}</strong>
            </div>
          ))}
          {nextAction ? <JsonBlock title="下一步动作" value={nextAction} defaultOpen={false} /> : null}
          <div className="plan-actions">
            <button className="secondary-button" type="button" onClick={() => onPlanAction("confirm-and-execute")}>
              <CheckCircle2 size={16} />
              确认并执行
            </button>
            <button className="secondary-button" type="button" onClick={() => onPlanAction("execute")}>
              <Play size={16} />
              继续执行
            </button>
            <button className="secondary-button" type="button" onClick={() => onPlanAction("resume")}>
              <RefreshCcw size={16} />
              恢复
            </button>
            <button className="secondary-button" type="button" onClick={() => onPlanAction("cancel")}>
              <XCircle size={16} />
              取消
            </button>
          </div>
        </div>
      ) : (
        <EmptyState icon={<GitBranch size={20} />} label="暂无计划" />
      )}
    </div>
  );
}

function EvidenceTab({ routeResponse }: { routeResponse: RouteResponse | null }) {
  const evidence = arrayContextValue(routeResponse, "evidence");
  const metadata = metadataValue(routeResponse, "evidence_provider_metadata");
  if (!evidence.length && !metadata) {
    return <EmptyState icon={<Eye size={20} />} label="暂无 Evidence 数据" />;
  }
  return (
    <div className="status-section">
      {metadata ? <JsonBlock title="Evidence Provider Metadata" value={metadata} /> : null}
      {evidence.length ? <JsonBlock title="Evidence" value={evidence} defaultOpen={!metadata} /> : null}
    </div>
  );
}

function ContextTab({ routeResponse }: { routeResponse: RouteResponse | null }) {
  const contextPack = contextPackFromRoute(routeResponse);
  if (!contextPack) {
    return <EmptyState icon={<Braces size={20} />} label="暂无 Context Pack 数据" />;
  }
  const usage = contextPack.usage;
  const budgetPercent = usage.budget_tokens > 0 ? Math.min(100, Math.round((usage.used_tokens / usage.budget_tokens) * 100)) : 0;
  const groups = groupContextSelection(contextPack.selection || []);
  return (
    <div className="status-section">
      <div className="context-pack-summary">
        <Metric label="预算使用" value={`${usage.used_tokens}/${usage.budget_tokens} tokens`} />
        <Metric label="使用率" value={`${budgetPercent}%`} />
        <Metric label="已选项目" value={String(usage.included_count)} />
        <Metric label="丢弃项目" value={String(usage.dropped_count)} />
      </div>
      <dl className="detail-list">
        <div>
          <dt>Pack</dt>
          <dd>{contextPack.pack_id}</dd>
        </div>
        <div>
          <dt>估算来源</dt>
          <dd>{usage.usage_source}</dd>
        </div>
        <div>
          <dt>裁剪</dt>
          <dd>
            {usage.truncated_count || usage.summary_placeholder_count
              ? `truncated=${usage.truncated_count}, summary=${usage.summary_placeholder_count}`
              : "-"}
          </dd>
        </div>
        <div>
          <dt>丢弃原因</dt>
          <dd>{formatReasonMap(usage.drop_reasons)}</dd>
        </div>
      </dl>
      <div className="context-group-list">
        {groups.map((group) => (
          <section className="context-group" key={group.source}>
            <div className="context-group-head">
              <strong>{group.source}</strong>
              <span>
                {group.included}/{group.items.length} included
              </span>
            </div>
            {group.items.map((item) => (
              <article className={`context-item-row ${item.included ? "included" : "dropped"}`} key={item.item_id}>
                <div>
                  <strong>{item.item_id}</strong>
                  <span>{item.role || item.scope}</span>
                </div>
                <div className="context-item-meta">
                  <span>{item.token_estimate} tok</span>
                  <span>{item.status}</span>
                  {item.drop_reason ? <span>{item.drop_reason}</span> : null}
                  {item.truncated ? <span>truncated</span> : null}
                  {item.agent_id ? <span>{item.agent_id}</span> : null}
                </div>
              </article>
            ))}
          </section>
        ))}
      </div>
      <JsonBlock title="Context Pack JSON" value={contextPack} defaultOpen={false} />
    </div>
  );
}

function MemoryTab({
  turn,
  routeResponse,
  onRefresh,
}: {
  turn: ConversationTurn | null;
  routeResponse: RouteResponse | null;
  onRefresh: () => void;
}) {
  const [operation, setOperation] = useState<MemoryManagementOperationResponse | null>(null);
  const [operationBusy, setOperationBusy] = useState("");
  const [operationError, setOperationError] = useState("");
  const memoryContext = turn?.memoryContext || null;
  const agentContext = turn?.agentContext || null;
  const legacyMemory = contextValue(routeResponse, "memory") || metadataValue(routeResponse, "memory");
  const traceState = turn?.memoryTrace;
  const traceData = traceState?.data;
  const recall = actualRecallRecords(traceData);
  const actualIds = new Set(recall.map((item) => item.memoryId));
  const considered = contextItems(memoryContext).filter(
    (item) => !actualIds.has(String(item.memory_id || item.item_id || "")),
  );
  const traces = uniqueFormationTraces(traceData?.formation_traces || []);
  const requestTrace = traceData?.request_trace || null;
  const formationCounts = formationDecisionCounts(traces);
  const errors = stringArrayValue(memoryContext?.errors);

  useEffect(() => {
    setOperation(null);
    setOperationError("");
    setOperationBusy("");
  }, [turn?.id]);

  async function resolveDecision(
    decision: MemoryFormationDecisionView,
    action: "confirm" | "reject",
  ) {
    if (!turn || !decision.decision_id) return;
    if (
      action === "confirm" &&
      decision.proposed_operation === "delete" &&
      !window.confirm("确认删除这条记忆？删除完成后正文与派生索引不可恢复。")
    ) {
      return;
    }
    const actionKey = `${action}:${decision.decision_id}`;
    setOperationBusy(actionKey);
    setOperationError("");
    try {
      const result = await api.resolvePendingMemory(
        decision.decision_id,
        action,
        {
          idempotency_key: `console:${action}:${decision.decision_id}`,
          reason: action === "confirm" ? "confirmed_in_conversation_console" : "rejected_in_conversation_console",
          expected_revision_id: decision.revision_id || null,
        },
        { userId: turn.userId, tenantId: turn.tenantId },
      );
      setOperation(result);
      onRefresh();
    } catch (error) {
      setOperationError(formatError(error));
    } finally {
      setOperationBusy("");
    }
  }

  async function deleteMemory(memoryId: string, revisionId?: string | null) {
    if (!turn) return;
    if (!window.confirm("确认删除这条记忆？删除完成后正文与派生索引不可恢复。")) return;
    const actionKey = `delete:${memoryId}`;
    setOperationBusy(actionKey);
    setOperationError("");
    try {
      const result = await api.deleteMemory(
        memoryId,
        {
          idempotency_key: `console:delete:${memoryId}`,
          reason: "deleted_in_conversation_console",
          expected_revision_id: revisionId || null,
        },
        { userId: turn.userId, tenantId: turn.tenantId },
      );
      setOperation(result);
      onRefresh();
    } catch (error) {
      setOperationError(formatError(error));
    } finally {
      setOperationBusy("");
    }
  }

  async function refreshOperation() {
    if (!turn || !operation?.index_operation_id) return;
    setOperationBusy("operation-status");
    setOperationError("");
    try {
      setOperation(
        await api.memoryOperation(operation.index_operation_id, {
          userId: turn.userId,
          tenantId: turn.tenantId,
        }),
      );
      onRefresh();
    } catch (error) {
      setOperationError(formatError(error));
    } finally {
      setOperationBusy("");
    }
  }

  if (!turn) {
    return <EmptyState icon={<Database size={20} />} label="选择一轮对话查看 Memory Trace" />;
  }

  return (
    <div className="memory-inspector">
      <section className="memory-inspector-section" aria-label="Recall Used">
        <div className="memory-section-head">
          <div>
            <span>Context Trace</span>
            <strong>Recall Used</strong>
          </div>
          <span className="memory-count">{recall.length}</span>
        </div>
        {traceState?.status === "loading" && !traceData ? (
          <MemorySectionState icon={<Loader2 className="spin" size={17} />} label="正在读取本轮 Recall Trace" />
        ) : recall.length ? (
          <div className="memory-record-list">
            {recall.map((record) => (
              <article className="memory-record" key={record.memoryId}>
                <div className="memory-record-main">
                  <strong>{record.memoryId}</strong>
                  <span>{record.item?.content || "正文不可用或已删除"}</span>
                </div>
                <div className="context-item-meta">
                  {record.item?.current_revision_id ? <span>{record.item.current_revision_id}</span> : null}
                  {record.item?.scope ? <span>{record.item.scope}</span> : null}
                  {record.item?.source ? <span>{record.item.source}</span> : null}
                  {typeof record.item?.confidence === "number" ? (
                    <span>confidence {record.item.confidence.toFixed(2)}</span>
                  ) : null}
                  {record.links.map((link) => (
                    <span key={`${link.consumer || "unknown"}:${link.projection_outcome || "included"}`}>
                      {link.consumer || "unknown"} / {link.projection_outcome || "included"}
                      {typeof link.relevance === "number" ? ` / relevance ${link.relevance.toFixed(2)}` : ""}
                      {typeof link.confidence === "number" ? ` / confidence ${link.confidence.toFixed(2)}` : ""}
                    </span>
                  ))}
                </div>
                {record.item?.canonical_refs?.length ? (
                  <span className="memory-reference">{record.item.canonical_refs.join(" · ")}</span>
                ) : null}
                {record.item?.memory_id ? (
                  <button
                    type="button"
                    className="icon-button small danger memory-row-action"
                    aria-label={`删除记忆 ${record.item.memory_id}`}
                    title="删除记忆"
                    disabled={operationBusy === `delete:${record.item.memory_id}`}
                    onClick={() => void deleteMemory(record.item!.memory_id, record.item!.current_revision_id)}
                  >
                    {operationBusy === `delete:${record.item.memory_id}` ? (
                      <Loader2 className="spin" size={15} />
                    ) : (
                      <Trash2 size={15} />
                    )}
                  </button>
                ) : null}
              </article>
            ))}
          </div>
        ) : (
          <MemorySectionState icon={<Database size={17} />} label="本轮没有实际进入 Router / Agent 的记忆" />
        )}
        {considered.length ? (
          <details className="memory-considered">
            <summary>Considered / dropped ({considered.length})</summary>
            <div className="debug-list compact">
              {considered.map((item, index) => (
                <article className="debug-row" key={String(item.memory_id || item.item_id || index)}>
                  <div>
                    <strong>{String(item.memory_id || item.item_id || `memory_${index + 1}`)}</strong>
                    <span>{String(item.content || "")}</span>
                  </div>
                  <div className="context-item-meta">
                    <span>{String(item.projection_outcome || "considered")}</span>
                    {item.scope ? <span>{String(item.scope)}</span> : null}
                  </div>
                </article>
              ))}
            </div>
          </details>
        ) : null}
      </section>

      <section className="memory-inspector-section" aria-label="Formation Write Decisions">
        <div className="memory-section-head">
          <div>
            <span>Formation Trace</span>
            <strong>Formation / Write Decisions</strong>
          </div>
          <button
            type="button"
            className="icon-button small"
            aria-label="刷新本轮 Memory Trace"
            title="刷新本轮 Memory Trace"
            onClick={onRefresh}
          >
            <RefreshCcw size={15} />
          </button>
        </div>
        <FormationStatus
          state={requestTrace?.overall_stage || traceState?.status || "idle"}
          counts={formationCounts}
        />
        {requestTrace ? <MemoryRequestTraceSummary trace={requestTrace} /> : null}
        {traceState?.error ? <div className="inline-error"><XCircle size={15} />{traceState.error}</div> : null}
        {traces.length ? (
          <div className="formation-trace-list">
            {traces.map((trace) => (
              <FormationTraceCard
                key={trace.job.job_id}
                trace={trace}
                busyKey={operationBusy}
                onResolve={resolveDecision}
              />
            ))}
          </div>
        ) : traceState?.status === "pending" ? (
          <MemorySectionState icon={<Loader2 className="spin" size={17} />} label="等待五轮窗口或空闲形成" />
        ) : traceState?.status === "not_triggered" ? (
          <MemorySectionState icon={<CircleDot size={17} />} label="本轮未触发 Formation Job" />
        ) : traceState?.status === "error" ? null : (
          <MemorySectionState icon={<Activity size={17} />} label="暂无 Formation Decision" />
        )}
        {operation ? (
          <div className={`operation-feedback ${operation.status}`} role="status">
            <div>
              <strong>{operation.operation}</strong>
              <span>{operation.status} / {operation.provider_status || "canonical"}</span>
            </div>
            {operation.index_operation_id ? (
              <button
                type="button"
                className="icon-button small"
                aria-label="刷新 Memory Operation 状态"
                onClick={() => void refreshOperation()}
              >
                {operationBusy === "operation-status" ? <Loader2 className="spin" size={15} /> : <RefreshCcw size={15} />}
              </button>
            ) : null}
          </div>
        ) : null}
        {operationError ? <div className="inline-error"><XCircle size={15} />{operationError}</div> : null}
      </section>

      {errors.length ? <ErrorList title="Memory Errors" errors={errors} /> : null}
      {traceData ? <JsonBlock title="Turn Memory Trace JSON" value={redactTurnMemoryTrace(traceData)} defaultOpen={false} /> : null}
      {agentContext ? <JsonBlock title="Agent Context Metadata" value={redactSensitive(agentContext)} defaultOpen={false} /> : null}
      {legacyMemory ? <JsonBlock title="Legacy Memory Metadata" value={redactSensitive(legacyMemory)} defaultOpen={false} /> : null}
    </div>
  );
}

function FormationTraceCard({
  trace,
  busyKey,
  onResolve,
}: {
  trace: MemoryFormationTraceView;
  busyKey: string;
  onResolve: (decision: MemoryFormationDecisionView, action: "confirm" | "reject") => void;
}) {
  return (
    <article className="formation-trace-card">
      <div className="formation-job-head">
        <div>
          <strong>{trace.job.job_id}</strong>
          <span>{trace.job.trigger} · {trace.job.mode}</span>
        </div>
        <span className={`formation-status ${trace.job.status}`}>{trace.job.status}</span>
      </div>
      <dl className="formation-job-meta">
        <div><dt>Source range</dt><dd>{trace.job.first_turn_id || "-"} → {trace.job.last_turn_id || "-"}</dd></div>
        <div><dt>Source refs</dt><dd>{trace.job.source_refs.length ? trace.job.source_refs.join(" · ") : "-"}</dd></div>
        <div><dt>Requests</dt><dd>{trace.links.request_ids.length ? trace.links.request_ids.join(" · ") : "-"}</dd></div>
        <div><dt>Runs</dt><dd>{trace.links.run_ids.length ? trace.links.run_ids.join(" · ") : "-"}</dd></div>
        <div><dt>Attempts</dt><dd>{trace.job.attempt_count}/{trace.job.max_attempts}</dd></div>
        <div><dt>Versions</dt><dd>{trace.job.model_version} · {trace.job.prompt_version} · {trace.job.policy_version}</dd></div>
        <div>
          <dt>Semantic</dt>
          <dd>
            {trace.semantic_contract_version || "-"} · validation {formatCountMap(trace.semantic_validation_counts)} · verifier {formatCountMap(trace.semantic_verifier_counts)}
          </dd>
        </div>
        <div><dt>Latency</dt><dd>{trace.model_latency_ms ?? "-"}ms model · {trace.provider_latency_ms ?? "-"}ms provider</dd></div>
      </dl>
      {trace.job.last_error_code ? <div className="inline-error"><AlertTriangle size={14} />{trace.job.last_error_code}</div> : null}
      {trace.decisions.length ? (
        <div className="formation-decisions">
          {trace.decisions.map((decision) => {
            const pending = decision.decision_status === "pending" && Boolean(decision.decision_id);
            const confirmable = pending && ["update", "delete"].includes(String(decision.proposed_operation));
            const contentRedacted = isSensitiveDecision(decision);
            return (
              <article className={`formation-decision ${decision.decision_status}`} key={decision.operation_id}>
                <div className="formation-decision-head">
                  <strong>{decision.proposed_operation || decision.operation}</strong>
                  <span>{decision.decision_status}</span>
                </div>
                <span className="memory-reference">{decision.reason_code}</span>
                <span className="memory-reference">{decision.memory_key}</span>
                {decision.canonical_refs.length ? (
                  <span className="memory-reference">{decision.canonical_refs.join(" · ")}</span>
                ) : null}
                {contentRedacted ? (
                  <p className="redacted-preview"><Shield size={14} />[redacted]</p>
                ) : decision.content_preview ? (
                  <p>{decision.content_preview.slice(0, 320)}</p>
                ) : null}
                <div className="context-item-meta">
                  {decision.scope ? <span>{decision.scope}</span> : null}
                  {decision.memory_id ? <span>{decision.memory_id}</span> : null}
                  {decision.revision_id ? <span>{decision.revision_id}</span> : null}
                  {decision.index_status ? <span>index {decision.index_status}</span> : null}
                  {decision.provider_status ? <span>provider {decision.provider_status}</span> : null}
                </div>
                {pending ? (
                  <div className="pending-actions">
                    {confirmable ? (
                      <button
                        type="button"
                        className="secondary-button compact"
                        disabled={busyKey === `confirm:${decision.decision_id}`}
                        onClick={() => onResolve(decision, "confirm")}
                      >
                        {busyKey === `confirm:${decision.decision_id}` ? <Loader2 className="spin" size={15} /> : <CheckCircle2 size={15} />}
                        确认
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="secondary-button compact"
                      disabled={busyKey === `reject:${decision.decision_id}`}
                      onClick={() => onResolve(decision, "reject")}
                    >
                      {busyKey === `reject:${decision.decision_id}` ? <Loader2 className="spin" size={15} /> : <XCircle size={15} />}
                      拒绝
                    </button>
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <MemorySectionState icon={<CircleDot size={16} />} label="Job 尚未产生决策" />
      )}
    </article>
  );
}

function MemorySectionState({ icon, label }: { icon: React.ReactNode; label: string }) {
  return <div className="memory-section-state">{icon}<span>{label}</span></div>;
}

function MemoryRequestTraceSummary({ trace }: { trace: NonNullable<MemoryDebugResponse["request_trace"]> }) {
  const links = [
    trace.turn_id,
    trace.formation_job_ids[0],
    trace.memory_ids[0],
    trace.index_operation_ids[0],
  ].filter(Boolean);
  return (
    <div className={`memory-request-trace ${trace.terminal ? "terminal" : trace.retryable ? "active" : ""}`}>
      <div>
        <strong>{formationStatusLabel(trace.overall_stage)}</strong>
        <span>{trace.reason_code || (trace.terminal ? "链路已到达终态" : "链路仍在推进")}</span>
      </div>
      {links.length ? <small>{links.join(" · ")}</small> : null}
    </div>
  );
}

function FormationStatus({ state, counts }: { state: string; counts: Record<string, number> }) {
  return (
    <div className="formation-summary">
      <span className={`formation-status ${state}`}>{formationStatusLabel(state)}</span>
      <div className="formation-counts">
        {Object.entries(counts).map(([operation, count]) => (
          <span key={operation}>{operation.toUpperCase()} {count}</span>
        ))}
      </div>
    </div>
  );
}

function KnowledgeTab({
  knowledgeContext,
  routeResponse,
}: {
  knowledgeContext: JsonRecord | null;
  routeResponse: RouteResponse | null;
}) {
  const agentContext = metadataValue(routeResponse, "agent_context");
  if (!knowledgeContext && !agentContext) {
    return <EmptyState icon={<BookOpen size={20} />} label="本轮没有可用 Knowledge Context" />;
  }
  const summary = contextTraceSummary(knowledgeContext);
  const items = contextItems(knowledgeContext);
  const citations = jsonRecordArray(knowledgeContext?.citations);
  const sourceIds = stringArrayValue(knowledgeContext?.source_ids);
  const metadata = recordValue(knowledgeContext?.metadata);
  const deniedSourceIds = stringArrayValue(metadata?.denied_source_ids);
  const errors = stringArrayValue(knowledgeContext?.errors);
  return (
    <div className="status-section">
      <ContextResultHeader title="Knowledge Context" status={summary.status} itemCount={summary.itemCount} />
      <div className="storage-boundary">
        <span>PostgreSQL source / chunk / log 是 canonical 数据</span>
        <span>Milvus 仅作为向量索引元数据</span>
      </div>
      {knowledgeContext?.summary ? <p className="context-summary-text">{String(knowledgeContext.summary)}</p> : null}
      <dl className="detail-list">
        <div>
          <dt>Source IDs</dt>
          <dd>{sourceIds.length ? sourceIds.join(", ") : "-"}</dd>
        </div>
        <div>
          <dt>Denied</dt>
          <dd>{deniedSourceIds.length ? deniedSourceIds.join(", ") : "-"}</dd>
        </div>
        <div>
          <dt>Vector</dt>
          <dd>{[metadata?.vector_backend, metadata?.collection].filter(Boolean).map(String).join(" / ") || "-"}</dd>
        </div>
      </dl>
      {items.length ? (
        <div className="debug-list">
          {items.map((item, index) => (
            <article className="debug-row" key={String(item.item_id || item.chunk_id || index)}>
              <div>
                <strong>{String(item.title || item.item_id || item.chunk_id || `knowledge_${index + 1}`)}</strong>
                <span>{String(item.content || "")}</span>
              </div>
              <div className="context-item-meta">
                {item.source_id ? <span>{String(item.source_id)}</span> : null}
                {numberValue(item.score) !== null ? <span>score {numberValue(item.score)?.toFixed(2)}</span> : null}
                {item.uri ? <span>{String(item.uri)}</span> : null}
              </div>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState icon={<BookOpen size={20} />} label={knowledgeContext ? "Knowledge Context 为空" : "本轮未返回 Knowledge Context"} />
      )}
      {citations.length ? <JsonBlock title="Citations" value={citations} defaultOpen={false} /> : null}
      {errors.length ? <ErrorList title="Knowledge Errors" errors={errors} /> : null}
      {agentContext ? <JsonBlock title="Agent Context Metadata" value={agentContext} defaultOpen={false} /> : null}
      {knowledgeContext ? <JsonBlock title="Knowledge Context JSON" value={knowledgeContext} defaultOpen={false} /> : null}
    </div>
  );
}

function ContextResultHeader({
  title,
  status,
  itemCount,
}: {
  title: string;
  status: string;
  itemCount: number;
}) {
  return (
    <div className="context-result">
      <div className="context-result-head">
        <strong>{title}</strong>
        <span>{status}</span>
      </div>
      <div className="decision-meta">
        <span>items: {itemCount}</span>
      </div>
    </div>
  );
}

function ErrorList({ title, errors }: { title: string; errors: string[] }) {
  return (
    <div className="error-list">
      <strong>{title}</strong>
      {errors.map((error) => (
        <span key={error}>{error}</span>
      ))}
    </div>
  );
}

function DebugTab({
  routeResponse,
  invokeResponse,
  eventJson,
  setEventJson,
  eventResponse,
  onSubmitEvent,
}: {
  routeResponse: RouteResponse | null;
  invokeResponse: RouteAndInvokeResponse["result"] | null;
  eventJson: string;
  setEventJson: (value: string) => void;
  eventResponse: JsonRecord | null;
  onSubmitEvent: () => void;
}) {
  const uiHandoff = invokeResponse?.output?.route ? invokeResponse.output : null;
  return (
    <div className="status-section">
      {uiHandoff ? <JsonBlock title="界面跳转" value={uiHandoff} defaultOpen={false} /> : null}
      {invokeResponse ? <JsonBlock title="调用结果" value={invokeResponse} defaultOpen={false} /> : null}
      {routeResponse ? <JsonBlock title="完整路由响应" value={routeResponse} defaultOpen={false} /> : null}
      <TextAreaField label="Agent 事件 JSON" value={eventJson} onChange={setEventJson} rows={8} />
      <button className="secondary-button" type="button" onClick={onSubmitEvent}>
        <Activity size={16} />
        提交事件
      </button>
      {eventResponse ? <JsonBlock title="事件响应" value={eventResponse} defaultOpen={false} /> : null}
      {!routeResponse && !invokeResponse && !eventResponse ? (
        <EmptyState icon={<FileJson size={20} />} label="暂无调试数据" />
      ) : null}
    </div>
  );
}

function DebugManagementPanel({ runtime }: { runtime: RuntimeConfig | null }) {
  const [activeView, setActiveView] = useState<"memory" | "knowledge">("memory");
  const [memoryFilters, setMemoryFilters] = useState<MemoryDebugFilters>({
    user_id: "",
    tenant_id: "",
    agent_id: "",
    scopes: "",
    memory_id: "",
    request_id: "",
    session_id: "",
    turn_id: "",
    run_id: "",
    formation_job_id: "",
    memory_key: "",
    decision_status: "",
    limit: "50",
  });
  const [knowledgeFilters, setKnowledgeFilters] = useState<KnowledgeDebugFilters>({
    source_ids: "",
    caller_type: "",
    caller_id: "",
    purpose: "",
    tenant_id: "",
    limit: "50",
  });
  const [memoryDebug, setMemoryDebug] = useState<MemoryDebugResponse | null>(null);
  const [knowledgeDebug, setKnowledgeDebug] = useState<KnowledgeDebugResponse | null>(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [knowledgeError, setKnowledgeError] = useState("");

  useEffect(() => {
    void loadMemoryDebug();
    void loadKnowledgeDebug();
  }, []);

  async function loadMemoryDebug(event?: FormEvent) {
    event?.preventDefault();
    setMemoryLoading(true);
    setMemoryError("");
    try {
      setMemoryDebug(await api.memoryDebug(memoryFilters));
    } catch (error) {
      setMemoryError(formatError(error));
    } finally {
      setMemoryLoading(false);
    }
  }

  async function loadKnowledgeDebug(event?: FormEvent) {
    event?.preventDefault();
    setKnowledgeLoading(true);
    setKnowledgeError("");
    try {
      setKnowledgeDebug(await api.knowledgeDebug(knowledgeFilters));
    } catch (error) {
      setKnowledgeError(formatError(error));
    } finally {
      setKnowledgeLoading(false);
    }
  }

  return (
    <section className="panel debug-management-panel">
      <div className="panel-title-row">
        <PanelTitle icon={<Database size={18} />} title="记忆与知识库调试管理" />
        <span className="read-only-chip">只读</span>
      </div>
      <RuntimeDebugSummary runtime={runtime} />
      <div className="segmented compact-segmented" role="tablist" aria-label="调试管理视图">
        <button
          type="button"
          role="tab"
          aria-selected={activeView === "memory"}
          className={activeView === "memory" ? "active" : ""}
          onClick={() => setActiveView("memory")}
        >
          <Database size={15} />
          Memory
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeView === "knowledge"}
          className={activeView === "knowledge" ? "active" : ""}
          onClick={() => setActiveView("knowledge")}
        >
          <BookOpen size={15} />
          Knowledge
        </button>
      </div>
      {activeView === "memory" ? (
        <MemoryDebugView
          filters={memoryFilters}
          setFilters={setMemoryFilters}
          data={memoryDebug}
          loading={memoryLoading}
          error={memoryError}
          onSubmit={loadMemoryDebug}
        />
      ) : (
        <KnowledgeDebugView
          filters={knowledgeFilters}
          setFilters={setKnowledgeFilters}
          data={knowledgeDebug}
          loading={knowledgeLoading}
          error={knowledgeError}
          onSubmit={loadKnowledgeDebug}
        />
      )}
    </section>
  );
}

function RuntimeDebugSummary({ runtime }: { runtime: RuntimeConfig | null }) {
  const memoryStatus = runtime?.memory_mem0_health_status || (runtime?.memory_mem0_degraded ? "degraded" : "-");

  return (
    <div className="runtime-debug-block">
      <div className="runtime-status-strip">
        <RuntimeStatus
          label="Memory"
          value={runtime ? runtime.memory_strategy_provider : "-"}
          active={Boolean(runtime?.memory_enabled) && memoryStatus === "ok"}
          status={runtime?.memory_enabled ? memoryStatus : "off"}
        />
        <RuntimeStatus
          label="Formation"
          value={runtime?.memory_formation_mode || "-"}
          active={Boolean(runtime?.memory_formation_worker_enabled)}
          status={runtime?.memory_formation_worker_enabled ? "worker on" : "off"}
        />
        <RuntimeStatus
          label="Knowledge"
          value={runtime?.knowledge_vector_backend || "-"}
          active={Boolean(runtime?.knowledge_enabled)}
          status={runtime?.knowledge_enabled ? "on" : "off"}
        />
      </div>
      <details className="debug-disclosure runtime-details">
        <summary><Settings2 size={15} />运行配置详情</summary>
        <div className="runtime-debug-summary">
          <Metric label="Memory Collection" value={runtime?.memory_mem0_collection || "-"} />
          <Metric label="Memory History" value={runtime?.memory_mem0_history_backend || "-"} />
          <Metric label="Formation Queue" value={runtime?.memory_formation_queue_status || "-"} />
          <Metric label="Formation Worker" value={runtime ? `${runtime.memory_formation_worker_enabled ? "on" : "off"} / ${runtime.memory_formation_sweeper_enabled ? "sweeper" : "no sweeper"}` : "-"} />
          <Metric label="Knowledge Index" value={runtime?.knowledge_milvus_collection || "-"} />
          <Metric label="Milvus Lite URI" value={runtime?.knowledge_milvus_uri || runtime?.memory_mem0_milvus_uri || "-"} />
        </div>
      </details>
    </div>
  );
}

function RuntimeStatus({ label, value, active, status }: { label: string; value: string; active: boolean; status: string }) {
  return (
    <div className="runtime-status">
      <span className={`runtime-status-dot ${active ? "active" : ""}`} aria-hidden="true" />
      <div>
        <strong>{label}</strong>
        <span>{value}</span>
      </div>
      <small>{status}</small>
    </div>
  );
}

function MemoryDebugView({
  filters,
  setFilters,
  data,
  loading,
  error,
  onSubmit,
}: {
  filters: MemoryDebugFilters;
  setFilters: (filters: MemoryDebugFilters) => void;
  data: MemoryDebugResponse | null;
  loading: boolean;
  error: string;
  onSubmit: (event?: FormEvent) => void;
}) {
  const [activeDataView, setActiveDataView] = useState<"items" | "formation" | "events">("items");
  const [selectedItem, setSelectedItem] = useState<MemoryDebugItem | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<MemoryDebugEvent | null>(null);
  const [selectedTrace, setSelectedTrace] = useState<MemoryFormationTraceView | null>(null);
  const update = (key: keyof MemoryDebugFilters, value: string) => setFilters({ ...filters, [key]: value });
  return (
    <div className="debug-management-view">
      <form className="debug-filter-form" onSubmit={onSubmit}>
        <TextField label="user_id" value={String(filters.user_id || "")} onChange={(value) => update("user_id", value)} />
        <TextField label="tenant_id" value={String(filters.tenant_id || "")} onChange={(value) => update("tenant_id", value)} />
        <TextField label="limit" value={String(filters.limit || "")} onChange={(value) => update("limit", value)} />
        <button type="submit" className="secondary-button" disabled={loading}>
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCcw size={16} />}
          刷新 Memory
        </button>
        <details className="debug-disclosure filter-disclosure">
          <summary><Settings2 size={15} />高级筛选</summary>
          <div className="advanced-filter-grid">
            <TextField label="agent_id" value={String(filters.agent_id || "")} onChange={(value) => update("agent_id", value)} />
            <TextField label="scopes" value={String(filters.scopes || "")} onChange={(value) => update("scopes", value)} />
            <TextField label="memory_id" value={String(filters.memory_id || "")} onChange={(value) => update("memory_id", value)} />
            <TextField label="request_id" value={String(filters.request_id || "")} onChange={(value) => update("request_id", value)} />
            <TextField label="session_id" value={String(filters.session_id || "")} onChange={(value) => update("session_id", value)} />
            <TextField label="turn_id" value={String(filters.turn_id || "")} onChange={(value) => update("turn_id", value)} />
            <TextField label="run_id" value={String(filters.run_id || "")} onChange={(value) => update("run_id", value)} />
            <TextField label="formation_job_id" value={String(filters.formation_job_id || "")} onChange={(value) => update("formation_job_id", value)} />
            <TextField label="memory_key" value={String(filters.memory_key || "")} onChange={(value) => update("memory_key", value)} />
            <SelectField
              label="decision_status"
              value={String(filters.decision_status || "")}
              onChange={(value) => update("decision_status", value)}
              options={["", "pending", "resolved", "accepted", "rejected"]}
            />
          </div>
        </details>
      </form>
      <ReadOnlyNotice />
      {error ? <div className="inline-error"><XCircle size={15} />{error}</div> : null}
      {data ? (
        <>
          <div className="context-pack-summary">
            <Metric label="Items" value={String(data.items.length)} />
            <Metric label="Events" value={String(data.events.length)} />
            <Metric label="Revisions" value={String((data.revisions || []).length)} />
            <Metric label="Formation Jobs" value={String((data.formation_traces || []).length)} />
            <Metric label="Provider" value={String(data.metadata.strategy_provider || data.metadata.memory_provider || "-")} />
            <Metric label="Status" value={String(recordValue(data.metadata.mem0)?.status || data.metadata.mem0_status || "-")} />
          </div>
          <div className="memory-data-tabs" role="tablist" aria-label="Memory 数据视图">
            <button type="button" role="tab" aria-selected={activeDataView === "items"} className={activeDataView === "items" ? "active" : ""} onClick={() => setActiveDataView("items")}>
              <Database size={15} />
              <span>Items</span>
              <strong>{data.items.length}</strong>
            </button>
            <button type="button" role="tab" aria-selected={activeDataView === "formation"} className={activeDataView === "formation" ? "active" : ""} onClick={() => setActiveDataView("formation")}>
              <GitBranch size={15} />
              <span>Formation</span>
              <strong>{uniqueFormationTraces(data.formation_traces || []).length}</strong>
            </button>
            <button type="button" role="tab" aria-selected={activeDataView === "events"} className={activeDataView === "events" ? "active" : ""} onClick={() => setActiveDataView("events")}>
              <Activity size={15} />
              <span>Events</span>
              <strong>{data.events.length}</strong>
            </button>
          </div>
          {activeDataView === "items" ? <section className="debug-section" role="tabpanel">
            <h3>Memory Items</h3>
            {data.items.length ? (
              <div className="debug-list">
                {data.items.map((item) => (
                  <button
                    type="button"
                    className="debug-row memory-item-row"
                    key={item.memory_id}
                    onClick={() => setSelectedItem(item)}
                    aria-label={`查看 Memory Item ${item.memory_id}`}
                  >
                    <span className="memory-item-main">
                      <strong className="memory-item-id" title={item.memory_id}>{item.memory_id}</strong>
                      <span className="memory-item-content">{item.content}</span>
                    </span>
                    <div className="context-item-meta">
                      <span>{item.scope}</span>
                      {item.user_id ? <span>{item.user_id}</span> : null}
                      {item.agent_id ? <span>{item.agent_id}</span> : null}
                      <Eye size={15} aria-hidden="true" />
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState icon={<Database size={20} />} label="当前过滤条件下没有 Memory Item" />
            )}
          </section> : null}
          {activeDataView === "formation" ? <section className="debug-section" role="tabpanel">
            <div className="debug-section-heading">
              <div>
                <h3>Formation Jobs</h3>
                <span>查看记忆形成任务及其决策链路</span>
              </div>
            </div>
            {(data.formation_traces || []).length ? (
              <div className="debug-list formation-job-list">
                {uniqueFormationTraces(data.formation_traces || []).map((trace) => (
                  <button type="button" className="formation-job-row" key={trace.job.job_id} onClick={() => setSelectedTrace(trace)} aria-label={`查看 Formation Job ${trace.job.job_id}`}>
                    <span className={`formation-job-status ${trace.job.status}`}>{trace.job.status}</span>
                    <span className="formation-job-main">
                      <strong title={trace.job.job_id}>{trace.job.job_id}</strong>
                      <span>{trace.job.first_turn_id || "-"} → {trace.job.last_turn_id || "-"}</span>
                    </span>
                    <span className="formation-job-facts">
                      <span>{trace.job.trigger} · {trace.job.mode}</span>
                      <span>{trace.decisions.length} decisions · {trace.model_latency_ms ?? "-"}ms</span>
                    </span>
                    <Eye size={16} aria-hidden="true" />
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState icon={<Activity size={20} />} label="当前过滤条件下没有 Formation Trace" />
            )}
          </section> : null}
          {activeDataView === "events" ? <section className="debug-section" role="tabpanel">
            <h3>Memory Events</h3>
            {data.events.length ? (
              <div className="debug-list compact">
                {data.events.map((event) => (
                  <button
                    type="button"
                    className="debug-row memory-item-row"
                    key={event.event_id}
                    onClick={() => setSelectedEvent(event)}
                    aria-label={`查看 Memory Event ${event.event_id}`}
                  >
                    <span className="memory-item-main">
                      <strong className="memory-item-id" title={event.event_type}>{event.event_type}</strong>
                      <span className="memory-item-id" title={event.memory_id || event.event_id}>{event.memory_id || event.event_id}</span>
                    </span>
                    <div className="context-item-meta">
                      {event.user_id ? <span>{event.user_id}</span> : null}
                      {event.agent_id ? <span>{event.agent_id}</span> : null}
                      <Eye size={15} aria-hidden="true" />
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState icon={<Activity size={20} />} label="当前过滤条件下没有 Memory Event" />
            )}
          </section> : null}
          <JsonBlock title="Memory Debug Metadata" value={redactSensitive(data.metadata)} defaultOpen={false} />
          {selectedItem ? (
            <MemoryItemDetailModal
              item={selectedItem}
              revisions={(data.revisions || []).filter((revision) => revision.memory_id === selectedItem.memory_id)}
              onClose={() => setSelectedItem(null)}
            />
          ) : null}
          {selectedEvent ? <MemoryEventDetailModal event={selectedEvent} onClose={() => setSelectedEvent(null)} /> : null}
          {selectedTrace ? <FormationTraceDetailModal trace={selectedTrace} onClose={() => setSelectedTrace(null)} /> : null}
        </>
      ) : (
        <EmptyState icon={<Database size={20} />} label={loading ? "正在读取 Memory Debug" : "尚未读取 Memory Debug"} />
      )}
    </div>
  );
}

function MemoryItemDetailModal({
  item,
  revisions,
  onClose,
}: {
  item: MemoryDebugItem;
  revisions: MemoryDebugResponse["revisions"];
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="panel editor-modal memory-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Memory Item 详情"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="panel-title-row sticky-modal-head">
          <PanelTitle icon={<Database size={18} />} title="Memory Item 详情" />
          <button className="icon-button small" type="button" onClick={onClose} aria-label="关闭 Memory Item 详情">
            <XCircle size={16} />
          </button>
        </div>
        <div className="memory-detail-heading">
          <strong title={item.memory_id}>{item.memory_id}</strong>
          <span>{item.scope}</span>
        </div>
        <div className="memory-detail-content">{item.content}</div>
        <div className="memory-detail-grid">
          <DetailField label="用户" value={item.user_id} />
          <DetailField label="租户" value={item.tenant_id} />
          <DetailField label="Agent" value={item.agent_id} />
          <DetailField label="置信度" value={typeof item.confidence === "number" ? item.confidence.toFixed(2) : null} />
          <DetailField label="重要度" value={typeof item.importance === "number" ? item.importance.toFixed(2) : null} />
          <DetailField label="可见范围" value={item.visibility} />
          <DetailField label="来源" value={item.source} />
          <DetailField label="生命周期" value={item.lifecycle_status} />
          <DetailField label="索引状态" value={item.index_status} />
          <DetailField label="当前版本" value={item.current_revision_no == null ? null : `rev ${item.current_revision_no}`} />
          <DetailField label="创建时间" value={item.created_at} />
          <DetailField label="更新时间" value={item.updated_at} />
        </div>
        {item.memory_key ? <DetailField label="Memory Key" value={item.memory_key} wide /> : null}
        {item.ttl_expires_at ? <DetailField label="TTL 到期时间" value={item.ttl_expires_at} wide /> : null}
        <section className="memory-detail-section">
          <div className="memory-detail-section-head">
            <div>
              <span>Version history</span>
              <strong>版本记录</strong>
            </div>
            <span className="memory-detail-count">{revisions.length}</span>
          </div>
          {revisions.length ? (
            <div className="memory-revision-list">
              {revisions.map((revision) => (
                <article className="memory-revision-row" key={revision.revision_id}>
                  <span className="revision-marker">{revision.revision_no}</span>
                  <div>
                    <strong title={revision.revision_id}>{revision.revision_id}</strong>
                    <span>{revision.content_redacted ? "[redacted]" : revision.content_preview || "无内容预览"}</span>
                  </div>
                  <div className="revision-facts">
                    <span>{revision.operation}</span>
                    <span>{revision.created_at}</span>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="memory-empty-inline"><GitBranch size={16} /><span>这条记忆还没有历史版本</span></div>
          )}
        </section>
        <JsonBlock title="完整数据" value={redactSensitive(item as unknown as JsonRecord)} defaultOpen={false} />
      </section>
    </div>
  );
}

function FormationTraceDetailModal({ trace, onClose }: { trace: MemoryFormationTraceView; onClose: () => void }) {
  const failed = ["failed", "dead_letter", "error"].includes(trace.job.status);
  const pending = trace.job.status === "pending";
  const persisted = trace.decisions.some((decision) => Boolean(decision.memory_id || decision.revision_id || decision.index_status));
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="panel editor-modal formation-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Formation Job 详情"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="panel-title-row sticky-modal-head">
          <PanelTitle icon={<GitBranch size={18} />} title="Formation Job 详情" />
          <button className="icon-button small" type="button" onClick={onClose} aria-label="关闭 Formation Job 详情">
            <XCircle size={16} />
          </button>
        </div>
        <div className="formation-detail-heading">
          <div>
            <strong title={trace.job.job_id}>{trace.job.job_id}</strong>
            <span>{trace.job.trigger} · {trace.job.mode}</span>
          </div>
          <span className={`formation-job-status ${trace.job.status}`}>{trace.job.status}</span>
        </div>
        <div className="formation-flow" aria-label="Formation 执行流程">
          <FormationFlowStep label="触发" detail={trace.job.trigger} state="complete" />
          <FormationFlowStep label="语义校验" detail={trace.semantic_contract_version || "未记录"} state={failed ? "failed" : "complete"} />
          <FormationFlowStep label="形成决策" detail={`${trace.decisions.length} decisions`} state={failed ? "failed" : trace.decisions.length ? "complete" : "pending"} />
          <FormationFlowStep label="持久化" detail={persisted ? "canonical / index" : pending ? "等待确认" : "未写入"} state={failed ? "failed" : persisted ? "complete" : "pending"} />
        </div>
        <div className="formation-detail-grid">
          <DetailField label="来源轮次" value={`${trace.job.first_turn_id || "-"} → ${trace.job.last_turn_id || "-"}`} />
          <DetailField label="尝试次数" value={`${trace.job.attempt_count}/${trace.job.max_attempts}`} />
          <DetailField label="候选数量" value={String(trace.candidate_count)} />
          <DetailField label="模型版本" value={trace.job.model_version} />
          <DetailField label="Prompt 版本" value={trace.job.prompt_version} />
          <DetailField label="策略版本" value={trace.job.policy_version} />
          <DetailField label="Validation" value={formatCountMap(trace.semantic_validation_counts)} />
          <DetailField label="Verifier" value={formatCountMap(trace.semantic_verifier_counts)} />
          <DetailField label="耗时" value={`${trace.model_latency_ms ?? "-"}ms model · ${trace.provider_latency_ms ?? "-"}ms provider`} />
        </div>
        <DetailField label="Source refs" value={trace.job.source_refs.join(" · ") || null} wide />
        <DetailField label="Requests / Runs" value={[...trace.links.request_ids, ...trace.links.run_ids].join(" · ") || null} wide />
        {trace.job.last_error_code ? <div className="inline-error formation-detail-error"><AlertTriangle size={15} />{trace.job.last_error_code}</div> : null}
        <section className="memory-detail-section">
          <div className="memory-detail-section-head">
            <div>
              <span>Decision trail</span>
              <strong>形成决策</strong>
            </div>
            <span className="memory-detail-count">{trace.decisions.length}</span>
          </div>
          {trace.decisions.length ? (
            <div className="formation-detail-decisions">
              {trace.decisions.map((decision) => (
                <article className={`formation-detail-decision ${decision.decision_status}`} key={decision.operation_id}>
                  <div className="formation-detail-decision-head">
                    <strong>{decision.proposed_operation || decision.operation}</strong>
                    <span>{decision.decision_status}</span>
                  </div>
                  <p>{decision.content_redacted ? "[redacted]" : decision.content_preview || "无内容预览"}</p>
                  <span className="memory-item-id" title={decision.memory_key}>{decision.memory_key}</span>
                  <div className="context-item-meta">
                    <span>{decision.reason_code}</span>
                    {decision.scope ? <span>{decision.scope}</span> : null}
                    {decision.revision_id ? <span>{decision.revision_id}</span> : null}
                    {decision.index_status ? <span>index {decision.index_status}</span> : null}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="memory-empty-inline"><CircleDot size={16} /><span>Job 尚未产生决策</span></div>
          )}
        </section>
        <JsonBlock title="完整 Formation Trace" value={redactSensitive(trace as unknown as JsonRecord)} defaultOpen={false} />
      </section>
    </div>
  );
}

function FormationFlowStep({ label, detail, state }: { label: string; detail: string; state: "complete" | "pending" | "failed" }) {
  return (
    <div className={`formation-flow-step ${state}`}>
      <span className="formation-flow-marker">{state === "complete" ? <CheckCircle2 size={15} /> : state === "failed" ? <XCircle size={15} /> : <CircleDot size={15} />}</span>
      <strong>{label}</strong>
      <span>{detail}</span>
    </div>
  );
}

function MemoryEventDetailModal({ event, onClose }: { event: MemoryDebugEvent; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="panel editor-modal memory-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Memory Event 详情"
        onMouseDown={(mouseEvent) => mouseEvent.stopPropagation()}
      >
        <div className="panel-title-row sticky-modal-head">
          <PanelTitle icon={<Activity size={18} />} title="Memory Event 详情" />
          <button className="icon-button small" type="button" onClick={onClose} aria-label="关闭 Memory Event 详情">
            <XCircle size={16} />
          </button>
        </div>
        <div className="memory-detail-heading">
          <strong title={event.event_type}>{event.event_type}</strong>
          <span>event</span>
        </div>
        <div className="memory-detail-grid memory-event-detail-grid">
          <DetailField label="Event ID" value={event.event_id} />
          <DetailField label="Memory ID" value={event.memory_id} />
          <DetailField label="用户" value={event.user_id} />
          <DetailField label="租户" value={event.tenant_id} />
          <DetailField label="Agent" value={event.agent_id} />
          <DetailField label="Scope" value={event.scope} />
          <DetailField label="Decision Status" value={event.decision_status} />
          <DetailField label="Decision ID" value={event.decision_id} />
          <DetailField label="Request ID" value={event.request_id} />
          <DetailField label="Session ID" value={event.session_id} />
          <DetailField label="Turn ID" value={event.turn_id} />
          <DetailField label="Run ID" value={event.run_id} />
          <DetailField label="Formation Job ID" value={event.formation_job_id} />
          <DetailField label="Memory Key" value={event.memory_key} />
          <DetailField label="创建时间" value={event.created_at} />
        </div>
        <JsonBlock title="Event Payload" value={redactSensitive(event.payload || {})} defaultOpen />
        <JsonBlock title="完整数据" value={redactSensitive(event as unknown as JsonRecord)} defaultOpen={false} />
      </section>
    </div>
  );
}

function DetailField({ label, value, wide = false }: { label: string; value?: string | null; wide?: boolean }) {
  return (
    <div className={`memory-detail-field ${wide ? "wide" : ""}`}>
      <span>{label}</span>
      <strong title={value || "-"}>{value || "-"}</strong>
    </div>
  );
}

function KnowledgeDebugView({
  filters,
  setFilters,
  data,
  loading,
  error,
  onSubmit,
}: {
  filters: KnowledgeDebugFilters;
  setFilters: (filters: KnowledgeDebugFilters) => void;
  data: KnowledgeDebugResponse | null;
  loading: boolean;
  error: string;
  onSubmit: (event?: FormEvent) => void;
}) {
  const update = (key: keyof KnowledgeDebugFilters, value: string) => setFilters({ ...filters, [key]: value });
  return (
    <div className="debug-management-view">
      <form className="debug-filter-form" onSubmit={onSubmit}>
        <TextField label="source_ids" value={String(filters.source_ids || "")} onChange={(value) => update("source_ids", value)} />
        <TextField label="tenant_id" value={String(filters.tenant_id || "")} onChange={(value) => update("tenant_id", value)} />
        <TextField label="limit" value={String(filters.limit || "")} onChange={(value) => update("limit", value)} />
        <button type="submit" className="secondary-button" disabled={loading}>
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCcw size={16} />}
          刷新 Knowledge
        </button>
        <details className="debug-disclosure filter-disclosure">
          <summary><Settings2 size={15} />高级筛选</summary>
          <div className="advanced-filter-grid">
            <TextField label="caller_type" value={String(filters.caller_type || "")} onChange={(value) => update("caller_type", value)} />
            <TextField label="caller_id" value={String(filters.caller_id || "")} onChange={(value) => update("caller_id", value)} />
            <TextField label="purpose" value={String(filters.purpose || "")} onChange={(value) => update("purpose", value)} />
          </div>
        </details>
      </form>
      <ReadOnlyNotice />
      <div className="storage-boundary">
        <span>PostgreSQL knowledge_sources / knowledge_chunks / knowledge_retrieval_logs 是 canonical 数据</span>
        <span>Milvus collection 只表示向量索引，不作为正文事实源</span>
      </div>
      {error ? <div className="inline-error"><XCircle size={15} />{error}</div> : null}
      {data ? (
        <>
          <div className="context-pack-summary">
            <Metric label="Sources" value={String(data.sources.length)} />
            <Metric label="Chunks" value={String(data.chunks.length)} />
            <Metric label="Logs" value={String(data.logs.length)} />
            <Metric label="Vector" value={String(data.metadata.vector_backend || "-")} />
          </div>
          <section className="debug-section">
            <h3>Knowledge Sources</h3>
            {data.sources.length ? (
              <div className="debug-list compact">
                {data.sources.map((source) => (
                  <article className="debug-row" key={source.source_id}>
                    <div>
                      <strong>{source.name || source.source_id}</strong>
                      <span>{source.description || source.source_id}</span>
                    </div>
                    <div className="context-item-meta">
                      <span>{source.enabled ? "enabled" : "disabled"}</span>
                      {source.tags?.map((tag) => <span key={tag}>{tag}</span>)}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState icon={<BookOpen size={20} />} label="当前过滤条件下没有 Knowledge Source" />
            )}
          </section>
          <section className="debug-section">
            <h3>Knowledge Chunks</h3>
            {data.chunks.length ? (
              <div className="debug-list">
                {data.chunks.map((chunk) => (
                  <article className="debug-row" key={chunk.chunk_id}>
                    <div>
                      <strong>{chunk.title || chunk.chunk_id}</strong>
                      <span>{chunk.content}</span>
                    </div>
                    <div className="context-item-meta">
                      <span>{chunk.chunk_id}</span>
                      <span>{chunk.source_id}</span>
                      {chunk.uri ? <span>{chunk.uri}</span> : null}
                      {chunk.updated_at ? <span>{chunk.updated_at}</span> : null}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState icon={<BookOpen size={20} />} label="当前过滤条件下没有 Knowledge Chunk" />
            )}
          </section>
          <section className="debug-section">
            <h3>Retrieval Logs</h3>
            {data.logs.length ? (
              <div className="debug-list compact">
                {data.logs.map((log) => (
                  <article className="debug-row" key={log.log_id}>
                    <div>
                      <strong>{log.query}</strong>
                      <span>{log.log_id}</span>
                    </div>
                    <div className="context-item-meta">
                      <span>{log.status || "-"}</span>
                      <span>{log.purpose}</span>
                      <span>hits {log.hit_count ?? 0}</span>
                      {(log.denied_source_ids || []).length ? <span>denied {(log.denied_source_ids || []).length}</span> : null}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState icon={<Activity size={20} />} label="当前过滤条件下没有 Retrieval Log" />
            )}
          </section>
          <JsonBlock title="Knowledge Debug Metadata" value={redactSensitive(data.metadata)} defaultOpen={false} />
        </>
      ) : (
        <EmptyState icon={<BookOpen size={20} />} label={loading ? "正在读取 Knowledge Debug" : "尚未读取 Knowledge Debug"} />
      )}
    </div>
  );
}

function ReadOnlyNotice() {
  return (
    <div className="inline-note">
      <Shield size={14} />
      <span>当前调试管理视图只读，不提供编辑、删除、上传、合并或 reindex 操作。</span>
    </div>
  );
}

function assistantTextFromRoute(route: RouteResponse): string {
  const assistantMessage = route.assistant_message?.trim();
  if (assistantMessage) return assistantMessage;
  const decisionMessage = route.decision.message?.trim();
  if (decisionMessage) return decisionMessage;
  if (route.decision.action === "silent") return "已收到。";
  return "路由完成。";
}

function chatStatusLabel(status: ChatMessage["status"]): string {
  if (status === "pending") return "处理中";
  if (status === "failed") return "失败";
  return "已完成";
}

function traceFromRoute(route: RouteResponse): {
  memoryContext: JsonRecord | null;
  knowledgeContext: JsonRecord | null;
  agentContext: JsonRecord | null;
} {
  return {
    memoryContext: recordValue(invocationInputValue(route, "memory_context")),
    knowledgeContext: recordValue(invocationInputValue(route, "knowledge_context")),
    agentContext: recordValue(metadataValue(route, "agent_context")),
  };
}

function contextTraceSummary(context: JsonRecord | null): {
  status: string;
  itemCount: number;
  citationCount: number;
  label: string;
} {
  if (!context) {
    return { status: "unavailable", itemCount: 0, citationCount: 0, label: "unavailable" };
  }
  const status = String(context.status || "unknown");
  const itemCount = contextItems(context).length;
  const citationCount = jsonRecordArray(context.citations).length;
  return {
    status,
    itemCount,
    citationCount,
    label: `${itemCount} / ${status}`,
  };
}

function contextAvailabilityLabel(turn: ConversationTurn): string {
  if (turn.assistantMessage.status === "failed") return "Trace failed";
  if (!turn.routeResponse) return "Trace unavailable";
  if (!turn.memoryContext && !turn.knowledgeContext) return "Context unavailable";
  const memoryStatus = contextTraceSummary(turn.memoryContext ?? null).status;
  const knowledgeStatus = contextTraceSummary(turn.knowledgeContext ?? null).status;
  return `${memoryStatus} / ${knowledgeStatus}`;
}

function deniedSourceCount(context: JsonRecord | null): number {
  const metadata = recordValue(context?.metadata);
  return stringArrayValue(metadata?.denied_source_ids).length;
}

function contextItems(context: JsonRecord | null): JsonRecord[] {
  return jsonRecordArray(context?.items);
}

function jsonRecordArray(value: unknown): JsonRecord[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is JsonRecord => Boolean(recordValue(item)));
}

function recordValue(value: unknown): JsonRecord | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as JsonRecord;
}

function stringArrayValue(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter(Boolean);
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function memoryTraceStatus(
  data: MemoryDebugResponse,
  mode: ExecutionMode,
  pollCount: number,
  formationMode?: string,
): ConversationTurn["memoryTrace"]["status"] {
  const requestTrace = data.request_trace;
  if (requestTrace) {
    if (requestTrace.overall_stage === "trace_missing") return "error";
    if (!requestTrace.terminal) return "pending";
    if (
      ["turn_failed", "outbox_dead_letter", "formation_dead_letter", "index_dead_letter", "trace_missing"].includes(
        requestTrace.overall_stage,
      )
    ) {
      return "error";
    }
    return "success";
  }
  const traces = uniqueFormationTraces(data.formation_traces || []);
  if (traces.some((trace) => trace.job.status === "dead_letter")) return "error";
  if (traces.some((trace) => ["pending", "claimed", "retry"].includes(trace.job.status))) {
    return "pending";
  }
  if (traces.length) return "success";
  if (formationMode === "off" || mode === "route") return "not_triggered";
  if (pollCount >= 20) return "pending";
  return "pending";
}

export function memoryTracePollDelay(pollCount: number): number {
  return Math.min(2000 * 2 ** Math.floor(Math.max(0, pollCount) / 5), 10_000);
}

function actualRecallRecords(data: MemoryDebugResponse | null | undefined): Array<{
  memoryId: string;
  links: MemoryTraceLink[];
  item?: MemoryDebugResponse["items"][number];
}> {
  const grouped = new Map<string, MemoryTraceLink[]>();
  for (const link of data?.context_trace_links || []) {
    if (link.projection_outcome !== "included") continue;
    for (const memoryId of link.memory_ids || []) {
      const links = grouped.get(memoryId) || [];
      if (!links.some((current) => current.consumer === link.consumer)) links.push(link);
      grouped.set(memoryId, links);
    }
  }
  return Array.from(grouped.entries()).map(([memoryId, links]) => ({
    memoryId,
    links,
    item: (data?.items || []).find((item) => item.memory_id === memoryId),
  }));
}

function formatCountMap(value: Record<string, number> | undefined): string {
  const entries = Object.entries(value || {}).sort(([left], [right]) => left.localeCompare(right));
  return entries.length ? entries.map(([key, count]) => `${key} ${count}`).join(" · ") : "-";
}

function uniqueFormationTraces(traces: MemoryFormationTraceView[]): MemoryFormationTraceView[] {
  const grouped = new Map<string, MemoryFormationTraceView>();
  for (const trace of traces) {
    const existing = grouped.get(trace.job.job_id);
    if (!existing) {
      grouped.set(trace.job.job_id, trace);
      continue;
    }
    const decisions = new Map(
      [...existing.decisions, ...trace.decisions].map((decision) => [decision.operation_id, decision]),
    );
    grouped.set(trace.job.job_id, {
      ...existing,
      ...trace,
      decisions: Array.from(decisions.values()),
      revision_ids: Array.from(new Set([...existing.revision_ids, ...trace.revision_ids])),
    });
  }
  return Array.from(grouped.values());
}

function formationDecisionCounts(traces: MemoryFormationTraceView[]): Record<string, number> {
  const counts: Record<string, number> = {};
  const seen = new Set<string>();
  for (const trace of traces) {
    for (const decision of trace.decisions || []) {
      if (seen.has(decision.operation_id)) continue;
      seen.add(decision.operation_id);
      const operation = String(decision.operation || "unknown").toLowerCase();
      counts[operation] = (counts[operation] || 0) + 1;
    }
  }
  return counts;
}

function formationStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: "未加载",
    loading: "加载中",
    pending: "等待中",
    success: "已完成",
    not_triggered: "未触发",
    error: "异常",
    turn_pending: "Turn 等待中",
    turn_running: "Agent 执行中",
    turn_failed: "Turn 失败",
    outbox_pending: "Outbox 等待中",
    outbox_retry: "Outbox 重试中",
    outbox_dead_letter: "Outbox 死信",
    formation_pending: "Formation 等待中",
    formation_retry: "Formation 重试中",
    formation_dead_letter: "Formation 死信",
    formation_skipped: "Formation 已跳过",
    completed_no_candidate: "无候选，已完成",
    policy_rejected: "策略已拒绝",
    memory_persisted_index_pending: "等待索引",
    index_retry: "索引重试中",
    index_dead_letter: "索引死信",
    persisted: "记忆已就绪",
    trace_missing: "链路缺失",
  };
  return labels[status] || status;
}

function isSensitiveDecision(decision: MemoryFormationDecisionView): boolean {
  return decision.content_redacted || String(decision.reason_code).toLowerCase().includes("sensitive");
}

function redactTurnMemoryTrace(data: MemoryDebugResponse): JsonValue {
  return redactSensitive({
    ...data,
    formation_traces: (data.formation_traces || []).map((trace) => ({
      ...trace,
      decisions: trace.decisions.map((decision) => ({
        ...decision,
        content_preview: isSensitiveDecision(decision)
          ? "[redacted]"
          : decision.content_preview?.slice(0, 320),
        content_redacted: isSensitiveDecision(decision),
      })),
    })),
  });
}

function redactSensitive(value: unknown): JsonValue {
  if (typeof value === "string") return redactClientText(value);
  if (value === null || ["number", "boolean"].includes(typeof value)) return value as JsonValue;
  if (Array.isArray(value)) {
    return value.map((item) => redactSensitive(item));
  }
  if (!value || typeof value !== "object") {
    return null;
  }
  const sanitized: JsonRecord = {};
  Object.entries(value as Record<string, unknown>).forEach(([key, child]) => {
    sanitized[key] = isSensitiveKey(key) ? "[redacted]" : redactSensitive(child);
  });
  return sanitized;
}

function isSensitiveKey(key: string): boolean {
  const normalized = key.toLowerCase();
  return (
    normalized.includes("api_key") ||
    normalized.includes("apikey") ||
    normalized.includes("password") ||
    normalized.includes("secret") ||
    normalized.includes("token") ||
    normalized.includes("credential") ||
    normalized.includes("prompt") ||
    normalized.includes("quote") ||
    normalized === "database_url" ||
    normalized.includes("connection_string") ||
    normalized.endsWith("_dsn")
  );
}

function redactClientText(value: string): string {
  return value
    .replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/:@]+:[^\s/@]+@/gi, "$1[redacted]@")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[redacted-email]")
    .replace(/\b\d{3}-\d{2}-\d{4}\b/g, "[redacted-identifier]")
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*\b/gi, "Bearer [redacted]");
}

function contextValue(routeResponse: RouteResponse | null, key: string): unknown {
  if (!routeResponse?.context) return null;
  return routeResponse.context[key] ?? null;
}

function metadataValue(routeResponse: RouteResponse | null, key: string): unknown {
  const metadata = contextValue(routeResponse, "metadata");
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return null;
  return (metadata as JsonRecord)[key] ?? null;
}

function invocationInputValue(routeResponse: RouteResponse | null, key: string): unknown {
  const invocation = routeResponse?.invocation;
  if (!invocation || typeof invocation !== "object" || Array.isArray(invocation)) return null;
  const input = (invocation as JsonRecord).input;
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  return (input as JsonRecord)[key] ?? null;
}

function contextPackFromRoute(routeResponse: RouteResponse | null): ContextPackDebug | null {
  const value = contextValue(routeResponse, "context_pack") || metadataValue(routeResponse, "context_pack");
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Partial<ContextPackDebug>;
  if (!candidate.usage || !candidate.budget || !Array.isArray(candidate.selection)) return null;
  return candidate as ContextPackDebug;
}

function groupContextSelection(selection: ContextSelectionDebug[]) {
  const bySource = new Map<string, ContextSelectionDebug[]>();
  for (const item of selection) {
    const current = bySource.get(item.source) || [];
    current.push(item);
    bySource.set(item.source, current);
  }
  return Array.from(bySource.entries()).map(([source, items]) => ({
    source,
    items,
    included: items.filter((item) => item.included).length,
  }));
}

function formatReasonMap(value: Record<string, number> | undefined): string {
  if (!value || !Object.keys(value).length) return "-";
  return Object.entries(value)
    .map(([reason, count]) => `${reason}: ${count}`)
    .join(", ");
}

function arrayContextValue(routeResponse: RouteResponse | null, key: string): unknown[] {
  const value = contextValue(routeResponse, key);
  return Array.isArray(value) ? value : [];
}

function PanelTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="panel-title">
      {icon}
      <h2>{title}</h2>
    </div>
  );
}

function StatusPill({ label, value, tone }: { label: string; value: string; tone: "good" | "warn" | "bad" }) {
  return (
    <span className={`status-pill ${tone}`}>
      {tone === "good" ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
      {label}: {value}
    </span>
  );
}

function adminModeLabel(runtime: RuntimeConfig | null): string {
  if (!runtime) return "-";
  if (runtime.admin_auth_mode === "local_loopback_open") return "本地免 Token";
  if (runtime.admin_auth_mode === "token_required") return "需要 Token";
  if (runtime.admin_auth_mode === "token_missing") return "缺少 Token";
  return runtime.admin_auth_mode;
}

function mutationModeLabel(runtime: RuntimeConfig | null): string {
  if (!runtime) return "-";
  if (runtime.registry_mutation_mode === "read_only_file") return "文件只读";
  if (runtime.registry_mutation_mode === "local_dev_write_enabled") return "本地可写";
  if (runtime.registry_mutation_mode === "token_required") return "Token 写入";
  if (runtime.registry_mutation_mode === "disabled_token_missing") return "写入禁用";
  return runtime.registry_mutation_mode;
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type={type} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

function TextAreaField({
  label,
  value,
  onChange,
  rows = 3,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <textarea rows={rows} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function JsonBlock({
  title,
  value,
  defaultOpen = true,
}: {
  title: string;
  value: unknown;
  defaultOpen?: boolean;
}) {
  return (
    <details className="json-block" open={defaultOpen}>
      <summary>
        <Braces size={15} />
        {title}
      </summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

function EmptyState({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <div className="empty-state">
      {icon}
      <span>{label}</span>
    </div>
  );
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinList(value: string[] | undefined): string {
  return (value || []).join(", ");
}

function parseJsonRecord(value: string, label: string): JsonRecord {
  try {
    const parsed = value.trim() ? JSON.parse(value) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`${label} 必须是 JSON object。`);
    }
    return parsed as JsonRecord;
  } catch (error) {
    if (error instanceof Error && error.message.includes("必须")) throw error;
    throw new Error(`${label} JSON 无效。`);
  }
}

function agentToForm(agent: AgentDefinition): FormState {
  return {
    agent_id: agent.agent_id,
    name: agent.name,
    description: agent.description,
    type: agent.type,
    enabled: agent.enabled,
    domain: agent.domain || "",
    version: agent.version || "",
    priority: String(agent.priority || 0),
    capabilities: joinList(agent.capabilities),
    tags: joinList(agent.tags),
    trigger_keywords: joinList(agent.trigger?.keywords),
    trigger_positive: joinList(agent.trigger?.positive_examples),
    trigger_negative: joinList(agent.trigger?.negative_examples),
    allow_roles: joinList(agent.access_policy?.allow_roles),
    allow_groups: joinList(agent.access_policy?.allow_groups),
    allow_tenants: joinList(agent.access_policy?.allow_tenants),
    required_inputs: joinList(agent.required_inputs),
    optional_inputs: joinList(agent.optional_inputs),
    input_schema: JSON.stringify(agent.input_schema || { type: "object", properties: {} }, null, 2),
    output_schema: JSON.stringify(agent.output_schema || { type: "object", properties: {} }, null, 2),
    invocation_config: JSON.stringify(agent.invocation?.config || {}, null, 2),
    provider_config: JSON.stringify(agent.invocation?.provider_config || {}, null, 2),
    ui_mode: agent.ui_handoff?.mode || "none",
    ui_route: agent.ui_handoff?.route || "",
    ui_params: JSON.stringify(agent.ui_handoff?.params || {}, null, 2),
    context: JSON.stringify(agent.context || { memory: { mode: "disabled" }, knowledge: { mode: "disabled" } }, null, 2),
    metadata: JSON.stringify(agent.metadata || {}, null, 2),
  };
}

function formToAgent(form: FormState): AgentDefinition {
  const requiredInputs = splitList(form.required_inputs);
  const inputSchema = parseJsonRecord(form.input_schema, "input_schema") as AgentDefinition["input_schema"];
  const outputSchema = parseJsonRecord(form.output_schema, "output_schema") as AgentDefinition["output_schema"];
  inputSchema.required = inputSchema.required || requiredInputs;
  return {
    agent_id: form.agent_id.trim(),
    name: form.name.trim(),
    description: form.description.trim(),
    version: form.version.trim() || null,
    enabled: form.enabled,
    type: form.type,
    capabilities: splitList(form.capabilities),
    domain: form.domain.trim() || null,
    tags: splitList(form.tags),
    trigger: {
      keywords: splitList(form.trigger_keywords),
      positive_examples: splitList(form.trigger_positive),
      negative_examples: splitList(form.trigger_negative),
    },
    access_policy: {
      allow_roles: splitList(form.allow_roles),
      allow_groups: splitList(form.allow_groups),
      allow_tenants: splitList(form.allow_tenants),
      deny_roles: [],
      deny_groups: [],
      deny_tenants: [],
      required_attributes: {},
    },
    required_inputs: requiredInputs,
    optional_inputs: splitList(form.optional_inputs),
    input_schema: inputSchema,
    output_schema: outputSchema,
    invocation: {
      type: form.type,
      config: parseJsonRecord(form.invocation_config, "invocation.config"),
      provider_config: parseJsonRecord(form.provider_config, "provider_config"),
    },
    ui_handoff: {
      mode: form.ui_mode.trim() || "none",
      route: form.ui_route.trim() || null,
      params: parseJsonRecord(form.ui_params, "ui params"),
    },
    context: parseJsonRecord(form.context, "context") as AgentDefinition["context"],
    priority: Number.parseInt(form.priority || "0", 10) || 0,
    metadata: parseJsonRecord(form.metadata, "metadata"),
    source: "database",
  };
}

function defaultEventJson(): string {
  return JSON.stringify(
    {
      event_id: "event_demo_001",
      session_id: "demo_session",
      agent_id: "script_writer",
      event_type: "agent_result",
      status: "completed",
      payload: { note: "话术生成演示事件已完成" },
    },
    null,
    2,
  );
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.status}: ${JSON.stringify(error.detail)}`;
  }
  if (error instanceof Error) {
    if (error.message === "Failed to fetch") {
      return "无法连接后端服务，或请求被浏览器/CORS/服务崩溃中断。请确认后端正在运行并查看后端日志。";
    }
    return error.message;
  }
  return "未知错误";
}

export default App;
