import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const mockAgent = {
  agent_id: "summarizer",
  name: "Summarizer",
  description: "Summarize text",
  version: "1.0.0",
  enabled: true,
  type: "mock",
  capabilities: ["summarize"],
  domain: "productivity",
  tags: ["text"],
  trigger: {
    keywords: ["summarize"],
    positive_examples: ["summarize this text"],
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
    properties: { summary: { type: "string" } },
  },
  invocation: {
    type: "mock",
    config: { response: { summary: "ok" } },
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
              decision: {
                status: "ok",
                action: "open_agent",
                target_agent_id: "summarizer",
                confidence: 0.7,
                reason: "test",
                message: "Routing to Summarizer.",
              },
              context: {
                candidate_agent_ids: ["summarizer"],
                evidence: [],
              },
              invocation: {
                mode: "deferred",
                agent_id: "summarizer",
                input: { text: body.input.text },
              },
            },
            result: {
              run_id: "run_1",
              agent_id: "summarizer",
              status: "completed",
              message: "ok",
              output: { summary: "ok" },
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
              candidate_agent_ids: ["summarizer"],
              evidence: [],
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
                  agent_id: "summarizer",
                  description: "总结文本",
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

    await userEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(screen.getAllByText("open_agent").length).toBeGreaterThan(0));
    expect(screen.getAllByText("summarizer").length).toBeGreaterThan(0);
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

    expect(await screen.findByText("plan_1")).toBeInTheDocument();
    expect(screen.getByText(/策略：require_confirmation/)).toBeInTheDocument();
    expect(screen.getByText(/下一步：confirm_plan/)).toBeInTheDocument();
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
