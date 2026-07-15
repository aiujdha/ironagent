import assert from "node:assert/strict";
import test from "node:test";
import { buildPostExecuteRequest, buildPreExecuteRequest, shouldExecuteTool } from "../security-hook.js";

test("buildPreExecuteRequest emits the expected hook envelope", () => {
  const request = buildPreExecuteRequest({
    capabilityProfile: "dangerous",
    toolName: "exec",
    toolArgs: { command: "ls" },
    riskLevel: "high",
  });

  assert.equal(request.envelope.protocol_version, "2026-04-03");
  assert.equal(request.envelope.hook_name, "pre_execute");
  assert.equal(request.tool.name, "exec");
  assert.equal(request.assistant.capability_profile, "dangerous");
});

test("buildPostExecuteRequest preserves the provided request id", () => {
  const request = buildPostExecuteRequest(
    {
      capabilityProfile: "onlyread",
      toolName: "read",
      toolArgs: { path: "/tmp/demo.txt" },
      success: true,
    },
    "request-123",
  );

  assert.equal(request.envelope.request_id, "request-123");
  assert.equal(request.envelope.hook_name, "post_execute");
});

test("shouldExecuteTool only allows immediate allow decisions", () => {
  assert.equal(
    shouldExecuteTool({
      protocol_version: "2026-04-03",
      request_id: "req-1",
      decision: "allow",
      reason: "ok",
      capability_profile: "onlyread",
      fail_closed: true,
      metadata: {},
    }),
    true,
  );

  assert.equal(
    shouldExecuteTool({
      protocol_version: "2026-04-03",
      request_id: "req-2",
      decision: "pending_approval",
      reason: "wait",
      capability_profile: "dangerous",
      fail_closed: true,
      metadata: {},
    }),
    false,
  );
});
