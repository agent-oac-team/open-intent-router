import type { AgentDefinition, ConversationTurn, JsonRecord } from "./types";

export type JourneyNodeId =
  | "input"
  | "context"
  | "routing"
  | "handoff"
  | "invocation"
  | "response"
  | "formation";

export type JourneyNodeState =
  | "waiting"
  | "active"
  | "attention"
  | "completed"
  | "skipped"
  | "failed";
export type JourneyOverallState = "empty" | "processing" | "completed" | "failed";

export type JourneyDetailField = {
  label: string;
  value: string;
};

export type JourneyStep = {
  id: string;
  label: string;
  agentName: string;
  status: string;
};

export type JourneySubstep = {
  id: string;
  label: string;
  summary: string;
  state: JourneyNodeState;
};

export type JourneyNode = {
  id: JourneyNodeId;
  label: string;
  summary: string;
  state: JourneyNodeState;
  tags?: string[];
  details: JourneyDetailField[];
  steps?: JourneyStep[];
  substeps?: JourneySubstep[];
};

export type JourneyProjection = {
  state: JourneyOverallState;
  title: string;
  summary: string;
  nodes: JourneyNode[];
};

type ContextSummary = {
  status: string;
  itemCount: number;
  citationCount: number;
  errors: string[];
};

const FORMATION_SUBSTEP_LABELS = [
  ["capture", "收集本轮对话"],
  ["queue", "进入后台队列"],
  ["candidates", "提取记忆候选"],
  ["decision", "语义校验与形成决策"],
  ["review", "等待人工处理"],
  ["persist", "更新长期记忆"],
  ["index", "更新检索索引"],
] as const;

const waitingNode = (id: JourneyNodeId, label: string, summary = "等待前序结果"): JourneyNode => ({
  id,
  label,
  summary,
  state: "waiting",
  details: [],
});

export function projectRoutingJourney(
  turn: ConversationTurn | null,
  agents: AgentDefinition[],
): JourneyProjection {
  if (!turn) {
    return {
      state: "empty",
      title: "等待一次真实请求",
      summary: "发送问题后，这里会用通俗方式还原中控的处理链路。",
      nodes: [],
    };
  }

  const inputNode: JourneyNode = {
    id: "input",
    label: "收到问题",
    summary: compactText(turn.userMessage.content, 72) || "已接收本轮用户输入",
    state: "completed",
    details: compactDetails([
      ["输入内容", turn.userMessage.content],
      ["执行模式", turn.mode === "route" ? "只路由" : "路由并调用"],
      ["提交时间", turn.userMessage.createdAt],
      ["请求标识", turn.requestId],
    ]),
  };

  if (turn.assistantMessage.status === "pending") {
    return {
      state: "processing",
      title: "中控处理中",
      summary: "正在理解问题并组织处理路径，内部阶段将在结果返回后按真实数据展示。",
      nodes: [
        inputNode,
        waitingNode("context", "准备参考信息", "结果返回后确认记忆与知识使用情况"),
        waitingNode("routing", "中控理解判断"),
        waitingNode("handoff", "交给业务助手"),
        waitingNode("invocation", "助手处理问题"),
        waitingNode("response", "返回处理结果"),
        {
          ...waitingNode("formation", "沉淀本次记忆", "等待本轮处理结果"),
          substeps: waitingFormationSubsteps(),
        },
      ],
    };
  }

  const route = turn.routeResponse || null;
  if (!route) {
    return {
      state: "failed",
      title: "本轮处理未完成",
      summary: turn.memoryTrace.error || "请求失败，未获得可用于还原内部链路的路由结果。",
      nodes: [
        inputNode,
        waitingNode("context", "准备参考信息", "未获得可确认结果"),
        waitingNode("routing", "中控理解判断", "未获得可确认结果"),
        waitingNode("handoff", "交给业务助手", "未进入可确认的交接阶段"),
        waitingNode("invocation", "助手处理问题", "未获得可确认结果"),
        {
          id: "response",
          label: "返回处理结果",
          summary: turn.assistantMessage.content || "请求失败",
          state: "failed",
          details: compactDetails([
            ["状态", turn.assistantMessage.status],
            ["错误信息", turn.assistantMessage.content],
          ]),
        },
        {
          id: "formation",
          label: "沉淀本次记忆",
          summary: "请求失败，本轮未进入记忆沉淀",
          state: "skipped",
          details: compactDetails([["原因", turn.memoryTrace.error || "请求未完成"]]),
        },
      ],
    };
  }

  const memory = summarizeContext(turn.memoryContext || null);
  const knowledge = summarizeContext(turn.knowledgeContext || null);
  const action = String(route.decision.action || "unknown");
  const targetAgentId = String(route.decision.target_agent_id || "").trim();
  const targetAgentName = agentName(targetAgentId, agents);
  const plan = asRecord(route.plan);
  const steps = projectPlanSteps(plan, agents);
  const hasHandoff = ["open_agent", "continue_agent"].includes(action) && Boolean(targetAgentId);

  const nodes: JourneyNode[] = [
    inputNode,
    contextNode(memory, knowledge),
    routingNode(route, targetAgentName, steps.length),
    handoffNode({ action, targetAgentId, targetAgentName, plan, steps, hasHandoff }),
    invocationNode(turn, action, hasHandoff, steps.length),
    responseNode(turn),
    formationNode(turn),
  ];
  const failed = nodes.some((node) => node.state === "failed");

  return {
    state: failed ? "failed" : "completed",
    title: failed ? "链路已还原，部分环节失败" : "本轮链路已还原",
    summary: journeySummary(action, targetAgentName, steps.length),
    nodes,
  };
}

function contextNode(memory: ContextSummary, knowledge: ContextSummary): JourneyNode {
  const recallTimedOut = memory.errors.includes("provider_timeout");
  const tags = [
    recallTimedOut
      ? "历史记忆读取超时"
      : memory.itemCount
        ? `历史记忆 ${memory.itemCount}`
        : "记忆未使用",
    knowledge.itemCount ? `知识资料 ${knowledge.itemCount}` : "知识未使用",
  ];
  let summary = recallTimedOut
    ? "历史记忆读取超时，本轮继续使用可用参考信息"
    : "本轮未使用额外记忆或知识资料";
  if (!recallTimedOut && memory.itemCount && knowledge.itemCount) {
    summary = `参考 ${memory.itemCount} 条历史记忆和 ${knowledge.itemCount} 条知识资料`;
  } else if (!recallTimedOut && memory.itemCount) {
    summary = `参考 ${memory.itemCount} 条历史记忆`;
  } else if (!recallTimedOut && knowledge.itemCount) {
    summary = `参考 ${knowledge.itemCount} 条知识资料`;
  }
  return {
    id: "context",
    label: "准备参考信息",
    summary,
    state: "completed",
    tags,
    details: compactDetails([
      ["记忆状态", memory.status],
      ["记忆条目", memory.itemCount],
      ["记忆读取异常", memory.errors.join("、")],
      ["知识状态", knowledge.status],
      ["知识条目", knowledge.itemCount],
      ["引用数量", knowledge.citationCount],
    ]),
  };
}

function routingNode(
  route: ConversationTurn["routeResponse"] extends infer T ? NonNullable<T> : never,
  targetAgentName: string,
  stepCount: number,
): JourneyNode {
  const action = String(route.decision.action || "unknown");
  const confidence = route.decision.confidence;
  return {
    id: "routing",
    label: "中控理解判断",
    summary: routeOutcomeSummary(action, targetAgentName, stepCount),
    state: route.decision.status === "error" ? "failed" : "completed",
    tags: [stepCount ? "生成协作计划" : routeActionLabel(action)],
    details: compactDetails([
      ["处理结果", routeActionLabel(action)],
      ["决策状态", route.decision.status],
      ["置信度", typeof confidence === "number" ? confidence.toFixed(2) : undefined],
      ["判断依据", route.decision.reason],
      ["候选助手", stringArray(route.context.candidate_agent_ids).join("、")],
      ["请求标识", route.request_id],
    ]),
  };
}

function handoffNode(input: {
  action: string;
  targetAgentId: string;
  targetAgentName: string;
  plan: JsonRecord | null;
  steps: JourneyStep[];
  hasHandoff: boolean;
}): JourneyNode {
  if (input.steps.length) {
    return {
      id: "handoff",
      label: "协调业务助手",
      summary: `已形成 ${input.steps.length} 个协作步骤`,
      state: "completed",
      tags: [`${input.steps.length} 个步骤`],
      steps: input.steps,
      details: compactDetails([
        ["计划标识", input.plan?.plan_id],
        ["计划状态", input.plan?.status],
        ["步骤数量", input.steps.length],
      ]),
    };
  }
  if (input.hasHandoff) {
    return {
      id: "handoff",
      label: "交给业务助手",
      summary: input.action === "continue_agent" ? `继续由 ${input.targetAgentName} 处理` : `交给 ${input.targetAgentName} 处理`,
      state: "completed",
      tags: [input.targetAgentName],
      details: compactDetails([
        ["助手名称", input.targetAgentName],
        ["Agent ID", input.targetAgentId],
        ["交接方式", routeActionLabel(input.action)],
      ]),
    };
  }
  return {
    id: "handoff",
    label: "交给业务助手",
    summary: skippedHandoffSummary(input.action),
    state: "skipped",
    details: compactDetails([["原因", skippedHandoffSummary(input.action)]]),
  };
}

function invocationNode(
  turn: ConversationTurn,
  action: string,
  hasHandoff: boolean,
  stepCount: number,
): JourneyNode {
  const result = turn.invokeResponse;
  if (result) {
    const status = String(result.status || "unknown");
    const state = invocationState(status);
    return {
      id: "invocation",
      label: "助手处理问题",
      summary: state === "failed" ? "业务助手处理失败" : state === "active" ? "业务助手仍在处理" : result.message || "业务助手已完成处理",
      state,
      tags: [status],
      details: compactDetails([
        ["执行状态", status],
        ["Agent ID", result.agent_id],
        ["运行标识", result.run_id],
        ["结果摘要", result.message],
        ["错误信息", asRecord(result.error)?.message],
      ]),
    };
  }
  let summary = "本轮没有调用业务助手";
  if (turn.mode === "route") summary = "仅判断处理路径，本轮未调用助手";
  else if (stepCount) summary = "协作计划已形成，尚未执行具体步骤";
  else if (hasHandoff) summary = "已完成交接判断，本次未获得调用结果";
  else summary = skippedHandoffSummary(action);
  return {
    id: "invocation",
    label: "助手处理问题",
    summary,
    state: "skipped",
    details: compactDetails([
      ["执行模式", turn.mode === "route" ? "只路由" : "路由并调用"],
      ["说明", summary],
    ]),
  };
}

function responseNode(turn: ConversationTurn): JourneyNode {
  const failed = turn.assistantMessage.status === "failed";
  return {
    id: "response",
    label: "返回处理结果",
    summary: compactText(turn.assistantMessage.content, 84) || (failed ? "未能返回处理结果" : "已向用户返回结果"),
    state: failed ? "failed" : "completed",
    details: compactDetails([
      ["返回状态", turn.assistantMessage.status],
      ["返回内容", turn.assistantMessage.content],
      ["返回时间", turn.assistantMessage.createdAt],
    ]),
  };
}

function formationNode(turn: ConversationTurn): JourneyNode {
  const trace = turn.memoryTrace;
  const requestTrace = trace.data?.request_trace;
  const substeps = formationSubsteps(turn);
  const pendingCount = substeps.filter((step) => step.state === "attention").length
    ? (trace.data?.formation_traces || []).flatMap((formation) => formation.decisions).filter(
        (decision) => decision.decision_status === "pending",
      ).length
    : 0;
  const details = compactDetails([
    ["形成状态", trace.status],
    ["链路阶段", requestTrace?.overall_stage],
    ["原因", requestTrace?.reason_code || trace.error],
    ["更新时间", requestTrace?.updated_at || trace.updatedAt],
    ["形成任务数", requestTrace?.formation_job_ids.length],
    ["记忆数量", requestTrace?.memory_ids.length],
  ]);
  if (pendingCount) {
    return {
      id: "formation",
      label: "沉淀本次记忆",
      summary: `有 ${pendingCount} 条记忆变更待处理`,
      state: "attention",
      tags: [`待处理 ${pendingCount}`],
      details,
      substeps,
    };
  }
  if (trace.status === "loading" || trace.status === "pending") {
    return {
      id: "formation",
      label: "沉淀本次记忆",
      summary: requestTrace ? formationStageLabel(requestTrace.overall_stage) : "正在确认是否需要沉淀长期记忆",
      state: "active",
      tags: ["后台处理中"],
      details,
      substeps,
    };
  }
  if (trace.status === "success") {
    const failed = substeps.some((step) => step.state === "failed");
    const active = substeps.some((step) => step.state === "active");
    return {
      id: "formation",
      label: "沉淀本次记忆",
      summary: pendingCount
        ? `有 ${pendingCount} 条记忆变更待处理`
        : failed && substeps.some((step) => step.id === "index" && step.state === "failed")
          ? "长期记忆已更新，检索索引未完成"
        : requestTrace
          ? formationStageLabel(requestTrace.overall_stage)
          : "记忆处理链路已完成",
      state: pendingCount ? "attention" : failed ? "failed" : active ? "active" : "completed",
      tags: pendingCount
        ? [`待处理 ${pendingCount}`]
        : acceptedFormationDecisions(turn).length
          ? [`本轮写入 ${acceptedFormationDecisions(turn).length} 条`]
          : ["已完成"],
      details,
      substeps,
    };
  }
  if (trace.status === "error") {
    return {
      id: "formation",
      label: "沉淀本次记忆",
      summary: trace.error || requestTrace?.reason_code || "记忆处理未完成",
      state: "failed",
      details,
      substeps,
    };
  }
  return {
    id: "formation",
    label: "沉淀本次记忆",
    summary: turn.mode === "route" ? "仅路由模式，本轮不沉淀对话记忆" : "本轮未触发长期记忆沉淀",
    state: "skipped",
    details,
    substeps,
  };
}

function waitingFormationSubsteps(): JourneySubstep[] {
  return FORMATION_SUBSTEP_LABELS.map(([id, label]) => ({
    id,
    label,
    summary: "等待本轮请求返回",
    state: "waiting",
  }));
}

function formationSubsteps(turn: ConversationTurn): JourneySubstep[] {
  const traceState = turn.memoryTrace;
  const data = traceState.data;
  const request = data?.request_trace;
  const traces = data?.formation_traces || [];
  if (turn.mode === "route" || (traceState.status === "not_triggered" && !request)) return [];

  const jobs = traces.map((trace) => trace.job);
  const decisions = traces.flatMap((trace) => trace.decisions);
  const pending = decisions.filter((decision) => decision.decision_status === "pending");
  const accepted = acceptedFormationDecisions(turn);
  const jobRunning = jobs.some((job) => ["pending", "claimed", "running", "retry"].includes(job.status));
  const jobFailed = jobs.some((job) => ["failed", "error", "dead_letter"].includes(job.status));
  const jobCompleted = jobs.length > 0 && jobs.every((job) =>
    ["completed", "success", "succeeded"].includes(job.status),
  );
  const candidateCount = traces.reduce((total, trace) => total + trace.candidate_count, 0);
  const stage = request?.overall_stage || "";
  const queueFailed = ["outbox_dead_letter", "turn_failed", "trace_missing"].includes(stage);
  const formationFailed = jobFailed || ["formation_dead_letter"].includes(stage);
  const formationRetry = jobRunning || stage === "formation_retry" || stage === "formation_pending";
  const captured = Boolean(
    request?.turn_id ||
      request?.formation_turn_ids.length ||
      request?.outbox_ids.length ||
      request?.formation_job_ids.length ||
      jobs.length,
  );
  const queued = Boolean(request?.formation_job_ids.length || jobs.length);
  const noWriteTerminal = Boolean(
    request?.terminal || jobCompleted || ["recall_only", "formation_skipped", "policy_rejected"].includes(stage),
  );

  const indexStates = accepted.map((decision) =>
    String(decision.index_status || decision.provider_status || "").toLowerCase(),
  );
  const indexFailed = indexStates.some((status) =>
    ["failed", "error", "dead_letter", "out_of_sync", "conflict"].includes(status),
  );
  const indexActive = indexStates.some((status) =>
    ["pending", "retry", "claimed", "running", "in_progress"].includes(status),
  );
  const indexCompleted = indexStates.length > 0 && indexStates.every((status) =>
    ["ready", "completed", "success", "succeeded", "deleted", "not_found"].includes(status),
  );

  return [
    {
      id: "capture",
      label: "收集本轮对话",
      summary: captured || request?.terminal ? "本轮对话已进入记忆链路" : "正在确认对话收集状态",
      state: captured || request?.terminal ? "completed" : traceState.status === "error" ? "failed" : "active",
    },
    {
      id: "queue",
      label: "进入后台队列",
      summary: queueFailed
        ? "后台投递未完成"
        : queued
          ? "后台任务已接收"
          : stage === "outbox_pending" || stage === "turn_captured"
            ? "等待后台任务接收"
            : "本轮无需后台形成任务",
      state: queueFailed
        ? "failed"
        : queued
          ? "completed"
          : stage === "outbox_pending" || stage === "turn_captured"
            ? "active"
            : noWriteTerminal
              ? "skipped"
              : "waiting",
    },
    {
      id: "candidates",
      label: "提取记忆候选",
      summary: formationFailed
        ? "候选提取未完成"
        : formationRetry
          ? stage === "formation_retry" || jobs.some((job) => job.status === "retry")
            ? "候选提取正在重试"
            : "正在分析可沉淀的信息"
          : candidateCount
            ? `已形成 ${candidateCount} 条候选`
            : "没有形成需要沉淀的候选",
      state: formationFailed
        ? "failed"
        : formationRetry
          ? "active"
          : candidateCount
            ? "completed"
            : noWriteTerminal
              ? "skipped"
              : "waiting",
    },
    {
      id: "decision",
      label: "语义校验与形成决策",
      summary: pending.length
        ? `${pending.length} 条变更需要人工处理`
        : decisions.length
          ? `已完成 ${decisions.length} 条形成决策`
          : formationFailed
            ? "形成决策未完成"
            : "没有需要执行的记忆变更",
      state: pending.length
        ? "attention"
        : decisions.length
          ? "completed"
          : formationFailed
            ? "failed"
            : formationRetry
              ? "active"
              : noWriteTerminal
                ? "skipped"
                : "waiting",
    },
    {
      id: "review",
      label: "等待人工处理",
      summary: pending.length ? `${pending.length} 条变更等待确认或拒绝` : "本轮无需人工处理",
      state: pending.length ? "attention" : "skipped",
    },
    {
      id: "persist",
      label: "更新长期记忆",
      summary: accepted.length
        ? `已接受并更新 ${accepted.length} 条长期记忆`
        : pending.length
          ? "等待人工处理后再更新"
          : "本轮没有写入长期记忆",
      state: accepted.length
        ? "completed"
        : pending.length || formationRetry
          ? "waiting"
          : noWriteTerminal || formationFailed
            ? "skipped"
            : "waiting",
    },
    {
      id: "index",
      label: "更新检索索引",
      summary: !accepted.length
        ? "本轮没有需要更新的检索索引"
        : indexFailed
          ? "检索索引更新未完成"
          : indexActive
            ? "正在更新检索索引"
            : indexCompleted
              ? "检索索引已更新"
              : "等待检索索引状态",
      state: !accepted.length
        ? pending.length || formationRetry
          ? "waiting"
          : "skipped"
        : indexFailed
          ? "failed"
          : indexActive
            ? "active"
            : indexCompleted
              ? "completed"
              : "waiting",
    },
  ];
}

function acceptedFormationDecisions(turn: ConversationTurn) {
  return (turn.memoryTrace.data?.formation_traces || [])
    .flatMap((trace) => trace.decisions)
    .filter(
      (decision) =>
        decision.decision_status === "accepted" &&
        ["add", "update", "delete"].includes(String(decision.operation).toLowerCase()),
    );
}

function summarizeContext(context: JsonRecord | null): ContextSummary {
  if (!context) return { status: "unavailable", itemCount: 0, citationCount: 0, errors: [] };
  return {
    status: String(context.status || "unknown"),
    itemCount: recordArray(context.items).length,
    citationCount: recordArray(context.citations).length,
    errors: stringArray(context.errors),
  };
}

function projectPlanSteps(plan: JsonRecord | null, agents: AgentDefinition[]): JourneyStep[] {
  return recordArray(plan?.steps).slice(0, 12).map((step, index) => {
    const agentId = String(step.agent_id || "").trim();
    return {
      id: String(step.step_id || `step-${index + 1}`),
      label: String(step.title || step.name || step.description || step.intent || `步骤 ${index + 1}`),
      agentName: agentName(agentId, agents),
      status: String(step.status || "pending"),
    };
  });
}

function agentName(agentId: string, agents: AgentDefinition[]): string {
  if (!agentId) return "业务助手";
  return agents.find((agent) => agent.agent_id === agentId)?.name || agentId;
}

function invocationState(status: string): JourneyNodeState {
  const normalized = status.toLowerCase();
  if (["failed", "error", "cancelled", "canceled", "timed_out"].includes(normalized)) return "failed";
  if (["pending", "running", "started", "accepted"].includes(normalized)) return "active";
  return "completed";
}

function journeySummary(action: string, agent: string, stepCount: number): string {
  if (stepCount) return `中控已形成 ${stepCount} 个协作步骤，并按返回结果展示当前进展。`;
  if (["open_agent", "continue_agent"].includes(action)) return `中控已判断本轮由 ${agent} 处理。`;
  if (action === "reply") return "本轮由中控直接形成答复，无需调用业务助手。";
  if (action === "clarify") return "中控需要用户补充信息后再继续处理。";
  if (action === "unsupported") return "当前注册能力中没有适合处理本轮问题的助手。";
  return `本轮处理结果：${routeActionLabel(action)}。`;
}

function routeOutcomeSummary(action: string, agent: string, stepCount: number): string {
  if (stepCount) return `识别为多步骤任务，已组织 ${stepCount} 个协作步骤`;
  if (action === "open_agent") return `选择 ${agent} 处理本轮问题`;
  if (action === "continue_agent") return `继续由 ${agent} 处理`;
  if (action === "reply") return "中控判断可以直接答复";
  if (action === "clarify") return "信息不足，需要用户补充说明";
  if (action === "unsupported") return "当前没有匹配的处理能力";
  if (action === "exit_agent") return "结束当前助手会话";
  if (action === "silent") return "本轮无需生成回复";
  return routeActionLabel(action);
}

function skippedHandoffSummary(action: string): string {
  if (action === "reply") return "中控直接答复，无需交给业务助手";
  if (action === "clarify") return "等待补充信息，暂不交给业务助手";
  if (action === "unsupported") return "没有合适的业务助手可供交接";
  if (action === "exit_agent") return "本轮结束当前助手会话";
  if (action === "silent") return "本轮无需调用业务助手";
  return "本轮未进入业务助手交接";
}

function routeActionLabel(action: string): string {
  const labels: Record<string, string> = {
    open_agent: "选择业务助手",
    continue_agent: "继续当前助手",
    reply: "中控直接答复",
    clarify: "请求补充信息",
    unsupported: "暂不支持",
    show_plan: "生成协作计划",
    exit_agent: "结束助手会话",
    silent: "无需回复",
  };
  return labels[action] || action || "未知结果";
}

function formationStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    recall_only: "已完成本轮记忆检查，无需新增记忆",
    turn_captured: "本轮对话已进入记忆处理队列",
    outbox_pending: "等待后台记忆任务处理",
    formation_pending: "正在分析本轮是否形成长期记忆",
    formation_completed: "长期记忆分析已完成",
    index_pending: "记忆已形成，正在更新检索索引",
    indexed: "长期记忆与检索索引已更新",
    persisted: "记忆处理结果已持久化",
    formation_retry: "记忆处理正在重试",
  };
  return labels[stage] || "记忆处理链路已更新";
}

function compactDetails(values: Array<[string, unknown]>): JourneyDetailField[] {
  return values.flatMap(([label, value]) => {
    if (value === undefined || value === null || value === "") return [];
    return [{ label, value: String(value) }];
  });
}

function compactText(value: string, maxLength: number): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength - 1)}…` : normalized;
}

function asRecord(value: unknown): JsonRecord | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as JsonRecord;
}

function recordArray(value: unknown): JsonRecord[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is JsonRecord => Boolean(asRecord(item)));
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter(Boolean);
}
