import {
  Activity,
  AlertTriangle,
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
  Shield,
  Trash2,
  XCircle,
  Zap,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { api, isApiError } from "./api";
import type {
  AgentDefinition,
  AgentListResponse,
  JsonRecord,
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
type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  status: "pending" | "completed" | "failed";
  createdAt: string;
  requestId?: string;
};
type StatusTab = "route" | "plan" | "context" | "memory" | "evidence" | "debug";

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
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
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
    const userMessageId = `msg_user_${timestamp}`;
    const assistantMessageId = `msg_assistant_${timestamp}`;
    const createdAt = new Date().toISOString();
    setChatMessages((current) => [
      ...current,
      { id: userMessageId, role: "user", content: text, status: "completed", createdAt },
      {
        id: assistantMessageId,
        role: "assistant",
        content: "正在判断意图...",
        status: "pending",
        createdAt,
      },
    ]);
    setMessage("");
    setBusy(true);
    try {
      const payload = buildRouteRequest(text);
      if (executionMode === "route") {
        const result = await api.route(payload);
        setRouteResponse(result);
        setInvokeResponse(null);
        if (result.plan && typeof result.plan === "object") setPlan(result.plan as JsonRecord);
        setChatMessages((current) =>
          current.map((item) =>
            item.id === assistantMessageId
              ? {
                  ...item,
                  content: assistantTextFromRoute(result),
                  status: "completed",
                  requestId: result.request_id,
                }
              : item,
          ),
        );
      } else {
        const result = await api.routeAndInvoke(payload);
        setRouteResponse(result.route);
        setInvokeResponse(result.result || null);
        if (result.route.plan && typeof result.route.plan === "object") setPlan(result.route.plan as JsonRecord);
        setChatMessages((current) =>
          current.map((item) =>
            item.id === assistantMessageId
              ? {
                  ...item,
                  content: assistantTextFromRoute(result.route),
                  status: "completed",
                  requestId: result.route.request_id,
                }
              : item,
          ),
        );
      }
    } catch (error) {
      setNotice(formatError(error));
      setChatMessages((current) =>
        current.map((item) =>
          item.id === assistantMessageId
            ? {
                ...item,
                content: "请求失败，请查看页面提示或右侧调试信息。",
                status: "failed",
              }
            : item,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  function startNewConversation() {
    const nextSessionId = `demo_${Date.now()}`;
    setSessionId(nextSessionId);
    setMessage("");
    setChatMessages([]);
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
      setPlan(await api.getPlan(targetPlanId));
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
        await api.planAction(targetPlanId, "cancel");
        setPlan(await api.getPlan(targetPlanId));
        return;
      }
      const payload = buildPlanExecutionPayload();
      const response =
        action === "confirm-and-execute"
          ? await api.confirmAndExecutePlan(targetPlanId, payload)
          : action === "resume"
            ? await api.resumePlan(targetPlanId, payload)
            : await api.executePlan(targetPlanId, payload);
      setPlan(response.plan);
      if (response.results[0]) {
        const first = response.results[0];
        setInvokeResponse({
          run_id: String(first.run_id || ""),
          agent_id: String(first.agent_id || ""),
          status: String(first.status || ""),
          message: String(first.message || ""),
          output: (first.output as JsonRecord | null) || null,
          artifact_refs: Array.isArray(first.artifact_refs) ? (first.artifact_refs as JsonRecord[]) : [],
          usage: {},
          error: (first.error as JsonRecord | null) || null,
        });
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
            messages={chatMessages}
          />
        </section>

        <aside className="right-rail">
          <StatusInspector
            routeResponse={routeResponse}
            invokeResponse={invokeResponse}
            plan={plan}
            planId={planId}
            setPlanId={setPlanId}
            eventJson={eventJson}
            setEventJson={setEventJson}
            eventResponse={eventResponse}
            onRefreshPlan={refreshPlan}
            onPlanAction={runPlanAction}
            onSubmitEvent={submitEvent}
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
  messages: ChatMessage[];
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
        <ChatTranscript messages={props.messages} />
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

function ChatTranscript({ messages }: { messages: ChatMessage[] }) {
  if (!messages.length) {
    return <EmptyState icon={<MessageSquareText size={20} />} label="选择演示问题或直接输入用户请求" />;
  }
  return (
    <div className="chat-transcript" aria-label="聊天记录">
      {messages.map((item) => (
        <article className={`chat-message ${item.role} ${item.status}`} key={item.id}>
          <div className="chat-avatar" aria-hidden="true">
            {item.role === "user" ? "U" : item.role === "assistant" ? "AI" : "S"}
          </div>
          <div className="chat-bubble">
            <div className="chat-meta">
              <span>{item.role === "user" ? "用户" : item.role === "assistant" ? "中控" : "系统"}</span>
              <strong>{chatStatusLabel(item.status)}</strong>
            </div>
            <p>{item.content}</p>
          </div>
        </article>
      ))}
    </div>
  );
}

function StatusInspector({
  routeResponse,
  invokeResponse,
  plan,
  planId,
  setPlanId,
  eventJson,
  setEventJson,
  eventResponse,
  onRefreshPlan,
  onPlanAction,
  onSubmitEvent,
}: {
  routeResponse: RouteResponse | null;
  invokeResponse: RouteAndInvokeResponse["result"] | null;
  plan: JsonRecord | null;
  planId: string;
  setPlanId: (value: string) => void;
  eventJson: string;
  setEventJson: (value: string) => void;
  eventResponse: JsonRecord | null;
  onRefreshPlan: () => void;
  onPlanAction: (action: "confirm-and-execute" | "execute" | "resume" | "cancel") => void;
  onSubmitEvent: () => void;
}) {
  const [activeTab, setActiveTab] = useState<StatusTab>("route");
  const tabs: Array<{ id: StatusTab; label: string; icon: React.ReactNode }> = [
    { id: "route", label: "Route", icon: <Route size={15} /> },
    { id: "plan", label: "Plan", icon: <ClipboardList size={15} /> },
    { id: "context", label: "Context", icon: <Braces size={15} /> },
    { id: "memory", label: "Memory", icon: <Database size={15} /> },
    { id: "evidence", label: "Evidence", icon: <Eye size={15} /> },
    { id: "debug", label: "Debug", icon: <FileJson size={15} /> },
  ];

  return (
    <section className="panel status-inspector">
      <PanelTitle icon={<Eye size={18} />} title="状态面板" />
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
        {activeTab === "route" ? (
          <RouteTab routeResponse={routeResponse} invokeResponse={invokeResponse} />
        ) : null}
        {activeTab === "plan" ? (
          <PlanTab
            plan={plan}
            planId={planId}
            setPlanId={setPlanId}
            onRefreshPlan={onRefreshPlan}
            onPlanAction={onPlanAction}
          />
        ) : null}
        {activeTab === "context" ? <ContextTab routeResponse={routeResponse} /> : null}
        {activeTab === "memory" ? <MemoryTab routeResponse={routeResponse} /> : null}
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
  if (!evidence.length) {
    return <EmptyState icon={<Eye size={20} />} label="暂无 Evidence 数据" />;
  }
  return (
    <div className="status-section">
      <JsonBlock title="Evidence" value={evidence} />
    </div>
  );
}

function ContextTab({ routeResponse }: { routeResponse: RouteResponse | null }) {
  const contextPack = contextValue(routeResponse, "context_pack") || metadataValue(routeResponse, "context_pack");
  if (!contextPack) {
    return <EmptyState icon={<Braces size={20} />} label="暂无 Context Pack 数据" />;
  }
  return (
    <div className="status-section">
      <JsonBlock title="Context Pack" value={contextPack} />
    </div>
  );
}

function MemoryTab({ routeResponse }: { routeResponse: RouteResponse | null }) {
  const memory = contextValue(routeResponse, "memory") || metadataValue(routeResponse, "memory");
  if (!memory) {
    return <EmptyState icon={<Database size={20} />} label="暂无 Memory 数据" />;
  }
  return (
    <div className="status-section">
      <JsonBlock title="Memory" value={memory} />
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

function contextValue(routeResponse: RouteResponse | null, key: string): unknown {
  if (!routeResponse?.context) return null;
  return routeResponse.context[key] ?? null;
}

function metadataValue(routeResponse: RouteResponse | null, key: string): unknown {
  const metadata = contextValue(routeResponse, "metadata");
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return null;
  return (metadata as JsonRecord)[key] ?? null;
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
