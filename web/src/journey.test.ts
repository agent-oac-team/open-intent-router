import { describe, expect, it } from "vitest";

import { projectRoutingJourney } from "./journey";
import type { AgentDefinition, ConversationTurn, JsonRecord } from "./types";

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
    expect(node(completed, "formation")).toMatchObject({ state: "completed", tags: ["关联 1 条记忆"] });
    expect(node(completedTurn(), "formation").state).toBe("skipped");
    expect(node(completedTurn({ memoryTrace: { status: "error", data: null, error: "入库失败", pollCount: 2 } }), "formation")).toMatchObject({ state: "failed", summary: "入库失败" });
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
