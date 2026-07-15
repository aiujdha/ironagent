import { describe, expect, it, vi } from "vitest";
import {
  __testing,
  handleAfterToolCall,
  handleBeforePromptBuild,
  handleBeforeToolCall,
  resolveIronAgentBridgeConfig,
} from "./api.js";

function createFetchResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn(async () => body),
  } as unknown as Response;
}

describe("ironagent bridge config", () => {
  it("stays disabled until the bridge flag is enabled", () => {
    const config = resolveIronAgentBridgeConfig({
      IRONAGENT_SERVICE_URL: "http://127.0.0.1:8000/",
    });

    expect(config.enabled).toBe(false);
    expect(config.baseUrl).toBe("http://127.0.0.1:8000");
    expect(config.capabilityProfile).toBe("onlyread");
  });
});

describe("ironagent bridge hooks", () => {
  it("blocks repeated admin-denied tool retries in the same run without reopening approval", async () => {
    __testing.rememberedDeniedAttempts.clear();
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        createFetchResponse({
          protocol_version: "2026-04-03",
          request_id: "req-denied-admin",
          decision: "pending_approval",
          reason: "tool requires approval",
          capability_profile: "dangerous",
          fail_closed: true,
          approval: {
            required: true,
            approval_id: "approval-denied-1",
            status: "pending",
            expires_at: new Date(Date.now() + 60_000).toISOString(),
          },
          metadata: {},
        }),
      )
      .mockResolvedValueOnce(
        createFetchResponse({
          approval_id: "approval-denied-1",
          tool_name: "exec",
          status: "denied",
          resolution: "deny",
          requested_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          resolved_at: new Date().toISOString(),
        }),
      )
      .mockResolvedValueOnce(
        createFetchResponse({
          protocol_version: "2026-04-03",
          request_id: "req-denied-admin",
          status: "ok",
        }),
      );

    const config = {
      enabled: true,
      baseUrl: "http://localhost:8000",
      timeoutMs: 5000,
      failClosed: true,
      capabilityProfile: "dangerous",
      approvalPollMs: 1,
      approvalMaxWaitMs: 300000,
    } as const;

    const firstResult = await handleBeforeToolCall(
      {
        toolName: "exec",
        params: { command: "touch /workspace/persistent/test.txt" },
        runId: "run-denied",
        toolCallId: "tool-denied-1",
      },
      {
        toolName: "exec",
        runId: "run-denied",
        toolCallId: "tool-denied-1",
        agentId: "demo-main",
        sessionKey: "agent:001:main",
      },
      {
        config,
        fetchImpl,
        sleep: async () => {},
      },
    );

    expect(firstResult).toEqual({
      block: true,
      blockReason: "denied in IronAgent",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(3);

    const secondResult = await handleBeforeToolCall(
      {
        toolName: "exec",
        params: { command: "touch /workspace/persistent/test.txt" },
        runId: "run-denied",
        toolCallId: "tool-denied-2",
      },
      {
        toolName: "exec",
        runId: "run-denied",
        toolCallId: "tool-denied-2",
        agentId: "demo-main",
        sessionKey: "agent:001:main",
      },
      {
        config,
        fetchImpl,
        sleep: async () => {},
      },
    );

    expect(secondResult).toEqual({
      block: true,
      blockReason: "denied in IronAgent. Do not retry the same action in this run.",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("injects the latest assistant capability profile into each prompt build", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValueOnce(
      createFetchResponse({
        assistant_id: "demo-main",
        name: "Demo Main Assistant",
        capability_profile: "onlywrite",
        notes: "demo",
      }),
    );

    const result = await handleBeforePromptBuild(
      {
        prompt: "read README",
        messages: [],
      },
      {
        agentId: "demo-main",
        sessionKey: "agent:main:main",
        sessionId: "session-1",
        runId: "run-0",
      },
      {
        config: {
          enabled: true,
          baseUrl: "http://localhost:8000",
          timeoutMs: 5000,
          failClosed: true,
          capabilityProfile: "onlyread",
          approvalPollMs: 1,
          approvalMaxWaitMs: 10,
        },
        fetchImpl,
      },
    );

    expect(result?.prependContext).toContain('Current IronAgent capability profile: "onlywrite"');
    expect(result?.prependContext).toContain("This capability profile can change between turns.");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("blocks denied tool calls and audits the blocked attempt", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        createFetchResponse({
          protocol_version: "2026-04-03",
          request_id: "req-deny",
          decision: "deny",
          reason: "tool is not permitted by capability profile",
          capability_profile: "onlyread",
          fail_closed: true,
          metadata: {},
        }),
      )
      .mockResolvedValueOnce(
        createFetchResponse({
          protocol_version: "2026-04-03",
          request_id: "req-deny",
          status: "ok",
        }),
      );

    const result = await handleBeforeToolCall(
      {
        toolName: "write",
        params: { path: "README.md" },
        runId: "run-1",
        toolCallId: "tool-1",
      },
      {
        toolName: "write",
        runId: "run-1",
        toolCallId: "tool-1",
        agentId: "demo-main",
      },
      {
        config: {
          enabled: true,
          baseUrl: "http://localhost:8000",
          timeoutMs: 5000,
          failClosed: true,
          capabilityProfile: "onlyread",
          approvalPollMs: 1,
          approvalMaxWaitMs: 10,
        },
        fetchImpl,
      },
    );

    expect(result).toEqual({
      block: true,
      blockReason: "tool is not permitted by capability profile",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("returns rewritten tool params when IronAgent auto-places an output path", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValueOnce(
      createFetchResponse({
        protocol_version: "2026-04-03",
        request_id: "req-place",
        decision: "allow",
        reason: "tool allowed by capability profile",
        capability_profile: "dangerous",
        fail_closed: true,
        metadata: {
          workspace_placement: {
            original_path: "report.docx",
            effective_path: "persistent/report.docx",
          },
        },
        tool_args: {
          path: "persistent/report.docx",
          content: "demo",
        },
      }),
    );

    const result = await handleBeforeToolCall(
      {
        toolName: "write",
        params: { path: "report.docx", content: "demo" },
        runId: "run-place",
        toolCallId: "tool-place",
      },
      {
        toolName: "write",
        runId: "run-place",
        toolCallId: "tool-place",
        agentId: "demo-main",
      },
      {
        config: {
          enabled: true,
          baseUrl: "http://localhost:8000",
          timeoutMs: 5000,
          failClosed: true,
          capabilityProfile: "dangerous",
          approvalPollMs: 1,
          approvalMaxWaitMs: 10,
        },
        fetchImpl,
      },
    );

    expect(result).toEqual({
      params: {
        path: "persistent/report.docx",
        content: "demo",
      },
    });
  });

  it("waits for IronAgent approval and reuses the pre-execute request id for audit", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        createFetchResponse({
          protocol_version: "2026-04-03",
          request_id: "req-approval",
          decision: "pending_approval",
          reason: "tool requires approval",
          capability_profile: "dangerous",
          fail_closed: true,
          approval: {
            required: true,
            approval_id: "approval-1",
            status: "pending",
            expires_at: new Date(Date.now() + 60_000).toISOString(),
          },
          metadata: {},
        }),
      )
      .mockResolvedValueOnce(
        createFetchResponse({
          approval_id: "approval-1",
          tool_name: "exec",
          status: "pending",
          resolution: null,
          requested_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          resolved_at: null,
        }),
      )
      .mockResolvedValueOnce(
        createFetchResponse({
          approval_id: "approval-1",
          tool_name: "exec",
          status: "approved",
          resolution: "allow",
          requested_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          resolved_at: new Date().toISOString(),
        }),
      )
      .mockResolvedValueOnce(
        createFetchResponse({
          protocol_version: "2026-04-03",
          request_id: "req-approval",
          status: "ok",
        }),
      );

    const config = {
      enabled: true,
      baseUrl: "http://localhost:8000",
      timeoutMs: 5000,
      failClosed: true,
      capabilityProfile: "dangerous",
      approvalPollMs: 1,
      approvalMaxWaitMs: 100,
    } as const;

    const beforeResult = await handleBeforeToolCall(
      {
        toolName: "exec",
        params: { command: "ls" },
        runId: "run-2",
        toolCallId: "tool-2",
      },
      {
        toolName: "exec",
        runId: "run-2",
        toolCallId: "tool-2",
        agentId: "demo-main",
      },
      {
        config,
        fetchImpl,
        sleep: async () => {},
      },
    );

    expect(beforeResult).toBeUndefined();

    await handleAfterToolCall(
      {
        toolName: "exec",
        params: { command: "ls" },
        runId: "run-2",
        toolCallId: "tool-2",
        result: { status: "ok" },
      },
      {
        toolName: "exec",
        runId: "run-2",
        toolCallId: "tool-2",
        agentId: "demo-main",
      },
      {
        config,
        fetchImpl,
      },
    );

    const lastCall = fetchImpl.mock.calls.at(-1);
    expect(lastCall?.[0]).toBe("http://localhost:8000/api/v1/hooks/post-execute");
    const requestBody = JSON.parse(String(lastCall?.[1]?.body ?? "{}")) as {
      envelope?: { request_id?: string };
      decision?: string;
    };
    expect(requestBody.envelope?.request_id).toBe("req-approval");
    expect(requestBody.decision).toBe("allow");
  });
});
