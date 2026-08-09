import { describe, expect, it } from "vitest";

import { projectRoutingJourney } from "./journey";
import type {
  AgentDefinition,
  ConversationTurn,
  JsonRecord,
  MemoryFormationDecisionView,
} from "./types";

const agents = [
  {
    agent_id: "script_writer",
    name: "话术生成助手",
  } as AgentDefinition,
];

function completedTurn(overrides: Partial<ConversationTurn> = {}): ConversationTurn {
  return {
    id: "turn_1",
    mode: "route-and-invoke",
    requestId: "req_1",
    userMessage: {
      id: "user_1",
      role: "user",
      content: "帮我生成一段客户邀约话术",
      status: "completed",
      createdAt: "2026-07-20T08:00:00Z",
    },
    assistantMessage: {
      id: "assistant_1",
      role: "assistant",
      content: "已经为你生成邀约话术。",
      status: "completed",
      createdAt: "2026-07-20T08:00:01Z",
      requestId: "req_1",
    },
    routeResponse: routeResponse("open_agent", "script_writer"),
    invokeResponse: {
      run_id: "run_1",
      agent_id: "script_writer",
      status: "completed",
      message: "话术生成完成",
      output: {},
    },
    memoryContext: null,
    knowledgeContext: null,
    agentContext: null,
    userId: "user_1",
    tenantId: "tenant_1",
    memoryTrace: { status: "not_triggered", data: null, pollCount: 0 },
    ...overrides,
  };
}

function routeResponse(action: string, targetAgentId: string | null = null, extra: JsonRecord = {}) {
  return {
    request_id: "req_1",
    session_id: "session_1",
    assistant_message: "处理完成",
    decision: {
      status: action === "clarify" ? "clarify" : action === "unsupported" ? "unsupported" : "ok",
      action,
      target_agent_id: targetAgentId,
      confidence: 0.92,
      reason: "测试路由判断",
      message: "处理完成",
    },
    context: {
      relation: "new_task",
      current_agent_id: null,
      artifact_refs: [],
      candidate_agent_ids: targetAgentId ? [targetAgentId] : [],
      intent_hint: null,
      evidence: [],
      metadata: {},
    },
    execution_policy: null,
    next_action: null,
    plan: null,
    invocation: null,
    error: null,
    ...extra,
  } as ConversationTurn["routeResponse"];
}

function node(turn: ConversationTurn, id: string) {
  return projectRoutingJourney(turn, agents).nodes.find((item) => item.id === id)!;
}

function formationData(input: {
  stage: string;
  jobStatus?: string;
  candidateCount?: number;
  decisions?: MemoryFormationDecisionView[];
  terminal?: boolean;
}) {
  const decisions = input.decisions || [];
  return {
    request_trace: {
      request_id: "req_1",
      overall_stage: input.stage,
      terminal: input.terminal ?? true,
      retryable: ["formation_pending", "formation_retry", "index_pending"].includes(input.stage),
      turn_id: "turn_1",
      turn_status: "completed",
      run_ids: ["run_1"],
      result_ids: ["result_1"],
      outbox_ids: ["outbox_1"],
      formation_turn_ids: ["turn_1"],
      formation_job_ids: ["job_1"],
      memory_ids: ["mem_existing"],
      revision_ids: ["revision_existing"],
      index_operation_ids: ["index_existing"],
    },
    items: [],
    revisions: [],
    events: [],
    formation_traces: [
      {
        job: {
          job_id: "job_1",
          trigger: "idle",
          status: input.jobStatus || "completed",
          mode: "enforced",
          first_turn_id: "turn_1",
          last_turn_id: "turn_1",
          source_refs: ["turn_1"],
          model_version: "formation-v1",
          prompt_version: "prompt-v1",
          policy_version: "policy-v1",
          attempt_count: input.jobStatus === "retry" ? 2 : 1,
          max_attempts: 5,
          created_at: "2026-07-20T08:00:01Z",
          updated_at: "2026-07-20T08:00:02Z",
        },
        links: {
          request_ids: ["req_1"],
          session_id: "session_1",
          turn_ids: ["turn_1"],
          run_ids: ["run_1"],
          memory_ids: ["mem_existing"],
        },
        scopes: ["user_preference"],
        decisions,
        candidate_count: input.candidateCount ?? decisions.length,
        decision_counts: {},
        semantic_validation_counts: {},
        semantic_verifier_counts: {},
        revision_ids: [],
        usage: {},
      },
    ],
    context_trace_links: [],
    metadata: {},
  };
}

function formationDecision(
  overrides: Partial<MemoryFormationDecisionView> = {},
): MemoryFormationDecisionView {
  return {
    decision_id: "decision_1",
    operation_id: "operation_1",
    operation: "add",
    proposed_operation: null,
    decision_status: "accepted",
    reason_code: "accepted_add",
    scope: "user_preference",
    memory_key: "tenant:tenant_1:user:user_1:preference:style",
    memory_id: "mem_existing",
    revision_id: "revision_existing",
    canonical_refs: ["turn:turn_1"],
    content_preview: "Use concise answers",
    content_redacted: false,
    index_status: "ready",
    provider_status: "success",
    ...overrides,
  };
}

describe("projectRoutingJourney", () => {
  it("为空态、处理中和请求失败提供诚实的整体状态", () => {
    expect(projectRoutingJourney(null, agents).state).toBe("empty");

    const pending = completedTurn({
      routeResponse: null,
      invokeResponse: null,
      assistantMessage: {
        ...completedTurn().assistantMessage,
        status: "pending",
        content: "中控正在处理...",
      },
      memoryTrace: { status: "loading", data: null, pollCount: 0 },
    });
    const first = projectRoutingJourney(pending, agents);
    const second = projectRoutingJourney(pending, agents);
    expect(first.state).toBe("processing");
    expect(first.nodes[0].state).toBe("completed");
    expect(first.nodes.slice(1).every((item) => item.state === "waiting")).toBe(true);
    expect(second.nodes.map((item) => item.state)).toEqual(first.nodes.map((item) => item.state));

    const failed = completedTurn({
      routeResponse: null,
      invokeResponse: null,
      assistantMessage: {
        ...completedTurn().assistantMessage,
        status: "failed",
        content: "请求失败",
      },
      memoryTrace: { status: "error", data: null, error: "网络错误", pollCount: 0 },
    });
    const failure = projectRoutingJourney(failed, agents);
    expect(failure.state).toBe("failed");
    expect(failure.nodes.find((item) => item.id === "routing")?.state).toBe("waiting");
    expect(failure.nodes.find((item) => item.id === "response")?.state).toBe("failed");
  });

  it("使用选中 turn 的 Memory、Knowledge、Agent 和调用结果", () => {
    const turn = completedTurn({
      memoryContext: { status: "ok", items: [{ memory_id: "mem_1" }] },
      knowledgeContext: {
        status: "ok",
        items: [{ item_id: "chunk_1" }],
        citations: [{ source_id: "guide" }],
      },
      memoryTrace: { status: "pending", data: null, pollCount: 2 },
    });
    const projection = projectRoutingJourney(turn, agents);
    expect(projection.summary).toContain("话术生成助手");
    expect(node(turn, "context").tags).toEqual(["历史记忆 1", "知识资料 1"]);
    expect(node(turn, "handoff").summary).toContain("话术生成助手");
    expect(node(turn, "invocation").state).toBe("completed");
    expect(node(turn, "formation").state).toBe("active");
  });

  it.each([
    ["reply", "中控直接答复"],
    ["clarify", "等待补充信息"],
    ["unsupported", "没有合适的业务助手"],
  ])("为 %s 结果跳过 Agent 链路", (action, expected) => {
    const turn = completedTurn({
      routeResponse: routeResponse(action),
      invokeResponse: null,
    });
    expect(node(turn, "routing").state).toBe("completed");
    expect(node(turn, "handoff").state).toBe("skipped");
    expect(node(turn, "handoff").summary).toContain(expected);
    expect(node(turn, "invocation").state).toBe("skipped");
    expect(node(turn, "response").state).toBe("completed");
  });

  it("区分只路由和调用失败，不把合法未调用标成失败", () => {
    const routeOnly = completedTurn({ mode: "route", invokeResponse: null });
    expect(node(routeOnly, "invocation")).toMatchObject({ state: "skipped" });
    expect(node(routeOnly, "invocation").summary).toContain("仅判断处理路径");

    const invokeFailed = completedTurn({
      invokeResponse: {
        run_id: "run_failed",
        agent_id: "script_writer",
        status: "failed",
        message: "调用超时",
        error: { code: "timeout", message: "下游超时", details: {} },
      },
    });
    expect(node(invokeFailed, "invocation").state).toBe("failed");
    expect(projectRoutingJourney(invokeFailed, agents).state).toBe("failed");
  });

  it("映射 Memory Formation 完成、未触发和失败状态", () => {
    const completed = completedTurn({
      memoryTrace: {
        status: "success",
        pollCount: 3,
        data: {
          request_trace: {
            request_id: "req_1",
            overall_stage: "indexed",
            terminal: true,
            retryable: false,
            run_ids: [],
            result_ids: [],
            outbox_ids: [],
            formation_turn_ids: [],
            formation_job_ids: ["job_1"],
            memory_ids: ["mem_1"],
            revision_ids: [],
            index_operation_ids: ["index_1"],
          },
          items: [],
          revisions: [],
          events: [],
          formation_traces: [],
          context_trace_links: [],
          metadata: {},
        },
      },
    });
    expect(node(completed, "formation")).toMatchObject({ state: "completed", tags: ["已完成"] });
    expect(node(completedTurn(), "formation").state).toBe("skipped");
    expect(node(completedTurn({ memoryTrace: { status: "error", data: null, error: "入库失败", pollCount: 2 } }), "formation")).toMatchObject({ state: "failed", summary: "入库失败" });
  });

  it("将 pending decision 标为待处理，不把旧 memory/index 引用当成本轮写入", () => {
    const turn = completedTurn({
      memoryTrace: {
        status: "pending",
        pollCount: 2,
        data: formationData({
          stage: "formation_pending",
          terminal: false,
          candidateCount: 1,
          decisions: [
            formationDecision({
              operation: "pending",
              proposed_operation: "update",
              decision_status: "pending",
              reason_code: "ambiguous_conflict",
              index_status: "ready",
            }),
          ],
        }),
      },
    });
    const formation = node(turn, "formation");
    expect(formation).toMatchObject({ state: "attention", summary: "有 1 条记忆变更待处理" });
    expect(formation.substeps?.find((step) => step.id === "review")?.state).toBe("attention");
    expect(formation.substeps?.find((step) => step.id === "persist")?.state).toBe("waiting");
    expect(formation.substeps?.find((step) => step.id === "index")?.state).toBe("waiting");
  });

  it("仅用 accepted lifecycle decision 投影本轮持久化和索引完成", () => {
    const turn = completedTurn({
      memoryTrace: {
        status: "success",
        pollCount: 3,
        data: formationData({ stage: "indexed", decisions: [formationDecision()] }),
      },
    });
    const formation = node(turn, "formation");
    expect(formation.tags).toEqual(["本轮写入 1 条"]);
    expect(formation.substeps?.find((step) => step.id === "persist")?.state).toBe("completed");
    expect(formation.substeps?.find((step) => step.id === "index")?.state).toBe("completed");
  });

  it("无候选和 reject/noop 终态会跳过写入与索引", () => {
    const noCandidate = completedTurn({
      memoryTrace: {
        status: "success",
        pollCount: 2,
        data: formationData({ stage: "formation_completed", candidateCount: 0, decisions: [] }),
      },
    });
    expect(node(noCandidate, "formation").substeps?.find((step) => step.id === "candidates")?.state).toBe("skipped");

    for (const decision of [
      formationDecision({ operation: "reject", decision_status: "rejected" }),
      formationDecision({ operation: "noop", decision_status: "accepted" }),
    ]) {
      const turn = completedTurn({
        memoryTrace: {
          status: "success",
          pollCount: 2,
          data: formationData({ stage: "policy_rejected", decisions: [decision] }),
        },
      });
      expect(node(turn, "formation").substeps?.find((step) => step.id === "persist")?.state).toBe("skipped");
      expect(node(turn, "formation").substeps?.find((step) => step.id === "index")?.state).toBe("skipped");
    }
  });

  it("区分 Formation retry、dead-letter 和索引失败", () => {
    const retry = completedTurn({
      memoryTrace: {
        status: "pending",
        pollCount: 2,
        data: formationData({ stage: "formation_retry", jobStatus: "retry", terminal: false }),
      },
    });
    expect(node(retry, "formation").substeps?.find((step) => step.id === "candidates")?.state).toBe("active");

    const deadLetter = completedTurn({
      memoryTrace: {
        status: "error",
        error: "provider_timeout",
        pollCount: 5,
        data: formationData({ stage: "formation_dead_letter", jobStatus: "dead_letter" }),
      },
    });
    expect(node(deadLetter, "formation").substeps?.find((step) => step.id === "candidates")?.state).toBe("failed");

    const indexFailed = completedTurn({
      memoryTrace: {
        status: "success",
        pollCount: 3,
        data: formationData({
          stage: "index_pending",
          decisions: [formationDecision({ index_status: "out_of_sync" })],
        }),
      },
    });
    expect(node(indexFailed, "formation").substeps?.find((step) => step.id === "index")?.state).toBe("failed");
  });

  it("将 Recall provider_timeout 留在准备参考信息阶段", () => {
    const turn = completedTurn({
      memoryContext: { status: "degraded", items: [], errors: ["provider_timeout"] },
      memoryTrace: {
        status: "success",
        pollCount: 2,
        data: formationData({ stage: "formation_completed", candidateCount: 0, decisions: [] }),
      },
    });
    expect(node(turn, "context").summary).toContain("历史记忆读取超时");
    expect(node(turn, "formation").substeps?.some((step) => step.summary.includes("超时"))).toBe(false);
  });

  it("请求未返回时所有 Formation 子步骤保持等待", () => {
    const pending = completedTurn({
      routeResponse: null,
      invokeResponse: null,
      assistantMessage: { ...completedTurn().assistantMessage, status: "pending" },
      memoryTrace: { status: "loading", data: null, pollCount: 0 },
    });
    const formation = node(pending, "formation");
    expect(formation.substeps).toHaveLength(7);
    expect(formation.substeps?.every((step) => step.state === "waiting")).toBe(true);
  });

  it("使用 Plan 的真实步骤，并为未知 Agent 保留安全回退", () => {
    const plan = {
      plan_id: "plan_1",
      status: "pending",
      steps: [
        { step_id: "step_1", description: "生成话术", agent_id: "script_writer", status: "completed" },
        { step_id: "step_2", description: "复核合规", agent_id: "very_long_unknown_agent_identifier", status: "pending" },
      ],
    };
    const turn = completedTurn({
      routeResponse: routeResponse("show_plan", null, { plan }),
      invokeResponse: null,
    });
    const handoff = node(turn, "handoff");
    expect(node(turn, "routing").tags).toEqual(["生成协作计划"]);
    expect(handoff.summary).toContain("2 个协作步骤");
    expect(handoff.steps?.[0]).toMatchObject({ label: "生成话术", agentName: "话术生成助手" });
    expect(handoff.steps?.[1].agentName).toBe("very_long_unknown_agent_identifier");
  });
});
