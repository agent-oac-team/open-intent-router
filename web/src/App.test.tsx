import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const mockAgent = {
  agent_id: "script_writer",
  name: "话术生成",
  description: "根据沟通目标生成客户沟通话术。",
  version: "1.0.0",
  enabled: true,
  type: "mock",
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
    properties: { draft: { type: "string" } },
  },
  invocation: {
    type: "mock",
    config: { response: { draft: "ok" } },
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

describe("意图路由测试台", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
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
          });
        }
        if (url.endsWith("/api/v1/agents")) return json({ agents: [mockAgent] });
        if (url.endsWith("/api/v1/route-and-invoke") && init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          return json({
            route: {
              request_id: "req_1",
              session_id: body.session_id,
              assistant_message: "我会交给话术生成处理。",
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
                input: { text: body.input.text },
              },
            },
            result: {
              run_id: "run_1",
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
    expect(within(transcript).queryByText("Routing to 话术生成.")).not.toBeInTheDocument();
    expect(screen.queryByText("第 1 轮")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("open_agent").length).toBeGreaterThan(0));
    expect(screen.getAllByText("script_writer").length).toBeGreaterThan(0);
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
});

function json(body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
