import { randomUUID } from "node:crypto";
import { resolveHookConfig, type HookConfig } from "./hook-config.js";
import { sendPostExecuteHook, sendPreExecuteHook } from "./hook-client.js";
import {
  HOOK_PROTOCOL_VERSION,
  type HookDecision,
  type PostExecuteHookRequest,
  type PostExecuteHookResponse,
  type PreExecuteHookRequest,
  type PreExecuteHookResponse,
  type RiskLevel,
  type ToolSource,
} from "./types.js";

export type IronAgentToolEvent = {
  assistantId?: string;
  assistantName?: string;
  capabilityProfile?: string;
  sessionId?: string;
  runId?: string;
  conversationId?: string;
  channel?: string;
  userId?: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  riskLevel?: RiskLevel;
  riskReason?: string;
  riskTags?: string[];
  toolSource?: ToolSource;
  optional?: boolean;
  ownerOnly?: boolean;
  metadata?: Record<string, unknown>;
};

export type IronAgentToolResultEvent = IronAgentToolEvent & {
  decision?: HookDecision;
  success: boolean;
  durationMs?: number;
  error?: string;
  outputSummary?: Record<string, unknown>;
};

function createEnvelope(hookName: "pre_execute" | "post_execute") {
  return {
    protocol_version: HOOK_PROTOCOL_VERSION,
    request_id: randomUUID(),
    source: "ironagent" as const,
    hook_name: hookName,
    occurred_at: new Date().toISOString(),
  };
}

export function buildPreExecuteRequest(event: IronAgentToolEvent): PreExecuteHookRequest {
  return {
    envelope: createEnvelope("pre_execute"),
    assistant: {
      assistant_id: event.assistantId,
      name: event.assistantName,
      capability_profile: event.capabilityProfile ?? "onlyread",
    },
    session: {
      session_id: event.sessionId,
      run_id: event.runId,
      conversation_id: event.conversationId,
      channel: event.channel,
      user_id: event.userId,
    },
    tool: {
      name: event.toolName,
      args: event.toolArgs,
      source: event.toolSource,
      optional: event.optional,
      owner_only: event.ownerOnly,
    },
    risk: {
      level: event.riskLevel ?? "medium",
      reason: event.riskReason,
      tags: event.riskTags ?? [],
    },
    metadata: event.metadata ?? {},
  };
}

export function buildPostExecuteRequest(
  event: IronAgentToolResultEvent,
  requestId?: string,
): PostExecuteHookRequest {
  return {
    envelope: {
      ...createEnvelope("post_execute"),
      ...(requestId ? { request_id: requestId } : {}),
    },
    assistant: {
      assistant_id: event.assistantId,
      name: event.assistantName,
      capability_profile: event.capabilityProfile ?? "onlyread",
    },
    session: {
      session_id: event.sessionId,
      run_id: event.runId,
      conversation_id: event.conversationId,
      channel: event.channel,
      user_id: event.userId,
    },
    tool: {
      name: event.toolName,
      args: event.toolArgs,
      source: event.toolSource,
      optional: event.optional,
      owner_only: event.ownerOnly,
    },
    result: {
      success: event.success,
      duration_ms: event.durationMs,
      error: event.error,
      output_summary: event.outputSummary ?? {},
    },
    decision: event.decision,
    metadata: event.metadata ?? {},
  };
}

function buildFailClosedResponse(request: PreExecuteHookRequest, reason: string): PreExecuteHookResponse {
  return {
    protocol_version: HOOK_PROTOCOL_VERSION,
    request_id: request.envelope.request_id,
    decision: "deny",
    reason,
    capability_profile: request.assistant.capability_profile,
    fail_closed: true,
    metadata: {},
  };
}

export async function runPreExecuteHook(
  event: IronAgentToolEvent,
  config: HookConfig = resolveHookConfig(),
): Promise<PreExecuteHookResponse> {
  const request = buildPreExecuteRequest(event);

  try {
    return await sendPreExecuteHook(request, config);
  } catch (error) {
    if (!config.failClosed) {
      return {
        protocol_version: HOOK_PROTOCOL_VERSION,
        request_id: request.envelope.request_id,
        decision: "allow",
        reason: `hook unavailable: ${String(error)}`,
        capability_profile: request.assistant.capability_profile,
        fail_closed: false,
        metadata: {},
      };
    }
    return buildFailClosedResponse(request, `hook unavailable: ${String(error)}`);
  }

export async function runPostExecuteHook(
  event: IronAgentToolResultEvent,
  requestId?: string,
  config: HookConfig = resolveHookConfig(),
): Promise<PostExecuteHookResponse | null> {
  const request = buildPostExecuteRequest(event, requestId);

  try {
    return await sendPostExecuteHook(request, config);
  } catch (error) {
    if (config.failClosed) {
      throw error;
    }
    return null;
  }

export function shouldExecuteTool(decision: PreExecuteHookResponse): boolean {
  return decision.decision === "allow";
}
