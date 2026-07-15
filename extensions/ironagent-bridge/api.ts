import { randomUUID } from "node:crypto";
import type {
  AgentHookContextLike,
  AfterToolCallEventLike,
  AssistantRecordResponse,
  ApprovalContext,
  ApprovalStatusResponse,
  BeforePromptBuildHookResult,
  BeforeToolCallEventLike,
  BeforeToolCallHookResult,
  HookDecision,
  IronAgentBridgeConfig,
  IronAgentBridgeLogger,
  PostExecuteHookRequest,
  PreExecuteHookRequest,
  PreExecuteHookResponse,
  RiskLevel,
  ToolHookContextLike,
} from "./types.js";
import { HOOK_PROTOCOL_VERSION } from "./types.js";

const MAX_TRACKED_DECISIONS = 1024;
const MAX_TRACKED_DENIALS = 1024;
const READ_TOOLS = new Set([
  "find",
  "glob",
  "grep",
  "ls",
  "memory_get",
  "memory_search",
  "read",
  "session_status",
  "sessions_history",
  "sessions_list",
  "web_fetch",
  "web_search",
]);
const WRITE_TOOLS = new Set(["apply_patch", "edit", "write"]);
const DANGEROUS_TOOLS = new Set([
  "code_execution",
  "cron",
  "exec",
  "gateway",
  "nodes",
  "process",
  "sessions_send",
  "sessions_spawn",
  "subagents",
]);

type FetchLike = typeof fetch;

type BridgeDeps = {
  fetchImpl?: FetchLike;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
};

type HandlerOptions = {
  config: IronAgentBridgeConfig;
  logger?: IronAgentBridgeLogger;
} & BridgeDeps;

type StoredDecision = {
  requestId: string;
  decision: HookDecision;
  capabilityProfile: string;
  metadata?: Record<string, unknown>;
};

const storedDecisions = new Map<string, StoredDecision>();
const rememberedDeniedAttempts = new Map<string, { reason: string; expiresAtMs: number }>();

function normalizeBaseUrl(raw: string): string {
  return raw.replace(/\/+$/, "");
}

function parsePositiveInt(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function trimToUndefined(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function truncateText(value: string, maxLength = 240): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

function safeLogger(logger?: IronAgentBridgeLogger): Required<IronAgentBridgeLogger> {
  return {
    debug: logger?.debug ?? (() => {}),
    info: logger?.info ?? (() => {}),
    warn: logger?.warn ?? (() => {}),
    error: logger?.error ?? (() => {}),
  };
}

function buildDecisionKey(params: { runId?: string; toolCallId?: string }): string | null {
  const toolCallId = params.toolCallId?.trim();
  if (!toolCallId) {
    return null;
  }
  return params.runId?.trim() ? `${params.runId}:${toolCallId}` : toolCallId;
}

function rememberDecision(
  params: { runId?: string; toolCallId?: string },
  decision: StoredDecision,
): void {
  const key = buildDecisionKey(params);
  if (!key) {
    return;
  }
  storedDecisions.set(key, decision);
  if (storedDecisions.size <= MAX_TRACKED_DECISIONS) {
    return;
  }
  const oldest = storedDecisions.keys().next().value;
  if (oldest) {
    storedDecisions.delete(oldest);
  }
}

function consumeDecision(params: {
  runId?: string;
  toolCallId?: string;
}): StoredDecision | undefined {
  const key = buildDecisionKey(params);
  if (!key) {
    return undefined;
  }
  const decision = storedDecisions.get(key);
  storedDecisions.delete(key);
  return decision;
}

function stableSerialize(value: unknown): string {
  if (value === null || value === undefined) {
    return "null";
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableSerialize(item)).join(",")}]`;
  }
  if (!isRecord(value)) {
    return JSON.stringify(String(value));
  }
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${stableSerialize(value[key])}`).join(",")}}`;
}

function buildDeniedAttemptKey(params: {
  runId?: string;
  sessionKey?: string;
  agentId?: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
}): string | null {
  const scope = params.runId?.trim() || params.sessionKey?.trim() || params.agentId?.trim();
  if (!scope) {
    return null;
  }
  return `${scope}:${params.toolName.trim().toLowerCase()}:${stableSerialize(params.toolArgs)}`;
}

function pruneDeniedAttempts(now: number): void {
  for (const [key, entry] of rememberedDeniedAttempts) {
    if (entry.expiresAtMs <= now) {
      rememberedDeniedAttempts.delete(key);
    }
  }
}

function rememberDeniedAttempt(params: {
  runId?: string;
  sessionKey?: string;
  agentId?: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  reason: string;
  expiresAtMs: number;
  now?: () => number;
}): void {
  const now = params.now ?? (() => Date.now());
  pruneDeniedAttempts(now());
  const key = buildDeniedAttemptKey(params);
  if (!key) {
    return;
  }
  rememberedDeniedAttempts.set(key, {
    reason: params.reason,
    expiresAtMs: params.expiresAtMs,
  });
  if (rememberedDeniedAttempts.size <= MAX_TRACKED_DENIALS) {
    return;
  }
  const oldest = rememberedDeniedAttempts.keys().next().value;
  if (oldest) {
    rememberedDeniedAttempts.delete(oldest);
  }
}

function consumeRememberedDeniedAttempt(params: {
  runId?: string;
  sessionKey?: string;
  agentId?: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  now?: () => number;
}): { reason: string } | undefined {
  const now = params.now ?? (() => Date.now());
  pruneDeniedAttempts(now());
  const key = buildDeniedAttemptKey(params);
  if (!key) {
    return undefined;
  }
  const denial = rememberedDeniedAttempts.get(key);
  if (!denial) {
    return undefined;
  }
  return { reason: denial.reason };
}

function buildAssistantContext(
  ctx: Pick<AgentHookContextLike, "agentId">,
  config: IronAgentBridgeConfig,
) {
  const assistantId = config.assistantId ?? ctx.agentId;
  return {
    assistant_id: assistantId,
    name: config.assistantName ?? assistantId,
    capability_profile: config.capabilityProfile,
  };
}

function buildSessionContext(
  ctx: Pick<AgentHookContextLike, "sessionId" | "runId" | "sessionKey">,
) {
  return {
    session_id: ctx.sessionId,
    run_id: ctx.runId,
    conversation_id: ctx.sessionKey,
  };
}

function buildCapabilityPromptHint(profile: string): string {
  const normalized = profile.trim().toLowerCase();
  let profileGuidance = `Current IronAgent capability profile: "${profile}".`;
  switch (normalized) {
    case "chat":
      profileGuidance =
        'Current IronAgent capability profile: "chat". No tools are available in this turn.';
      break;
    case "onlyread":
      profileGuidance =
        'Current IronAgent capability profile: "onlyread". Read-only tools such as `read`, `web_search`, `web_fetch`, and session inspection tools are available. File mutation and shell execution are not available.';
      break;
    case "onlywrite":
      profileGuidance =
        'Current IronAgent capability profile: "onlywrite". File inspection and file editing tools such as `read`, `write`, `edit`, and `apply_patch` are available. Shell execution is not available.';
      break;
    case "dangerous":
      profileGuidance =
        'Current IronAgent capability profile: "dangerous". High-risk tools may be available, but some actions can still require approval before execution.';
      break;
    default:
      break;
  }
  return [
    profileGuidance,
    "This capability profile can change between turns.",
    "If a prior turn said a tool was denied, do not assume that result still applies now.",
    "For this turn, decide whether to use tools based on the current profile and rely on actual tool execution results.",
  ].join("\n");
}

function inferRisk(toolName: string): { level: RiskLevel; reason: string; tags: string[] } {
  const normalized = toolName.trim().toLowerCase();
  if (DANGEROUS_TOOLS.has(normalized)) {
    const level: RiskLevel =
      normalized === "gateway" || normalized === "nodes" ? "critical" : "high";
    return {
      level,
      reason: "dangerous tool requires approval-focused evaluation",
      tags: ["dangerous", normalized],
    };
  }
  if (WRITE_TOOLS.has(normalized)) {
    return {
      level: "medium",
      reason: "write-capable tool modifies project state",
      tags: ["write", normalized],
    };
  }
  if (READ_TOOLS.has(normalized)) {
    return {
      level: "low",
      reason: "read-only tool",
      tags: ["read", normalized],
    };
  }
  return {
    level: "medium",
    reason: "tool not yet classified, treating as medium risk",
    tags: ["unclassified", normalized],
  };
}

function summarizeToolResult(result: unknown): Record<string, unknown> {
  if (result === null || result === undefined) {
    return {};
  }
  if (typeof result === "string") {
    return {
      type: "string",
      preview: truncateText(result),
    };
  }
  if (typeof result === "number" || typeof result === "boolean") {
    return {
      type: typeof result,
      value: result,
    };
  }
  if (Array.isArray(result)) {
    return {
      type: "array",
      length: result.length,
    };
  }
  if (!isRecord(result)) {
    return {
      type: typeof result,
    };
  }

  const summary: Record<string, unknown> = {
    type: "object",
    keys: Object.keys(result).slice(0, 12),
  };
  for (const field of ["status", "message", "error", "path", "command", "id"]) {
    const value = result[field];
    if (typeof value === "string" && value.trim()) {
      summary[field] = truncateText(value);
    }
  }
  return summary;
}

function buildPreExecuteRequest(
  event: BeforeToolCallEventLike,
  ctx: ToolHookContextLike,
  config: IronAgentBridgeConfig,
): PreExecuteHookRequest {
  const risk = inferRisk(event.toolName);
  return {
    envelope: {
      protocol_version: HOOK_PROTOCOL_VERSION,
      request_id: randomUUID(),
      source: "ironagent",
      hook_name: "pre_execute",
      occurred_at: new Date().toISOString(),
    },
    assistant: buildAssistantContext(ctx, config),
    session: buildSessionContext(ctx),
    tool: {
      name: event.toolName,
      args: isRecord(event.params) ? event.params : {},
    },
    risk: {
      level: risk.level,
      reason: risk.reason,
      tags: risk.tags,
    },
    metadata: {
      ironagent: {
        session_key: ctx.sessionKey,
        tool_call_id: event.toolCallId ?? ctx.toolCallId,
      },
    },
  };
}

function buildPostExecuteRequest(params: {
  event: AfterToolCallEventLike;
  ctx: ToolHookContextLike;
  config: IronAgentBridgeConfig;
  requestId?: string;
  decision?: HookDecision;
  capabilityProfile?: string;
  decisionMetadata?: Record<string, unknown>;
  success: boolean;
  error?: string;
  outputSummary?: Record<string, unknown>;
}): PostExecuteHookRequest {
  return {
    envelope: {
      protocol_version: HOOK_PROTOCOL_VERSION,
      request_id: params.requestId ?? randomUUID(),
      source: "ironagent",
      hook_name: "post_execute",
      occurred_at: new Date().toISOString(),
    },
    assistant: {
      ...buildAssistantContext(params.ctx, params.config),
      capability_profile: params.capabilityProfile ?? params.config.capabilityProfile,
    },
    session: buildSessionContext(params.ctx),
    tool: {
      name: params.event.toolName,
      args: isRecord(params.event.params) ? params.event.params : {},
    },
    result: {
      success: params.success,
      duration_ms: params.event.durationMs,
      error: params.error,
      output_summary: params.outputSummary ?? summarizeToolResult(params.event.result),
    },
    decision: params.decision,
    metadata: {
      ...(params.decisionMetadata ?? {}),
      ironagent: {
        session_key: params.ctx.sessionKey,
        tool_call_id: params.event.toolCallId ?? params.ctx.toolCallId,
      },
    },
  };
}

async function requestJson<TResponse>(params: {
  path: string;
  method: "GET" | "POST";
  config: IronAgentBridgeConfig;
  body?: unknown;
  fetchImpl?: FetchLike;
}): Promise<TResponse> {
  const fetchImpl = params.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), params.config.timeoutMs);

  try {
    const response = await fetchImpl(new URL(params.path, params.config.baseUrl).toString(), {
      method: params.method,
      headers: {
        ...(params.body !== undefined ? { "content-type": "application/json" } : {}),
        ...(params.config.apiToken ? { authorization: `Bearer ${params.config.apiToken}` } : {}),
      },
      ...(params.body !== undefined ? { body: JSON.stringify(params.body) } : {}),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`IronAgent request failed with status ${response.status}`);
    }
    return (await response.json()) as TResponse;
  } finally {
    clearTimeout(timeout);
  }
}

async function fetchAssistantRecord(params: {
  ctx: AgentHookContextLike;
  config: IronAgentBridgeConfig;
  fetchImpl?: FetchLike;
}): Promise<AssistantRecordResponse | undefined> {
  const assistantId = params.config.assistantId ?? params.ctx.agentId;
  if (!assistantId?.trim()) {
    return undefined;
  }
  try {
    return await requestJson<AssistantRecordResponse>({
      path: `/api/v1/assistants/${encodeURIComponent(assistantId)}`,
      method: "GET",
      config: params.config,
      fetchImpl: params.fetchImpl,
    });
  } catch {
    return undefined;
  }
}

async function reportBlockedAttempt(params: {
  config: IronAgentBridgeConfig;
  logger?: IronAgentBridgeLogger;
  fetchImpl?: FetchLike;
  event: BeforeToolCallEventLike;
  ctx: ToolHookContextLike;
  requestId: string;
  capabilityProfile: string;
  reason: string;
  decisionMetadata?: Record<string, unknown>;
}): Promise<void> {
  try {
    await requestJson({
      path: "/api/v1/hooks/post-execute",
      method: "POST",
      config: params.config,
      fetchImpl: params.fetchImpl,
      body: buildPostExecuteRequest({
        event: {
          toolName: params.event.toolName,
          params: params.event.params,
          runId: params.event.runId,
          toolCallId: params.event.toolCallId,
        },
        ctx: params.ctx,
        config: params.config,
        requestId: params.requestId,
        decision: "deny",
        capabilityProfile: params.capabilityProfile,
        decisionMetadata: params.decisionMetadata,
        success: false,
        error: params.reason,
        outputSummary: { blocked: true, reason: params.reason },
      }),
    });
  } catch (error) {
    safeLogger(params.logger).warn(`IronAgent blocked-attempt audit failed: ${String(error)}`);
  }
}

async function waitForApprovalResolution(params: {
  approval: ApprovalContext;
  config: IronAgentBridgeConfig;
  logger?: IronAgentBridgeLogger;
  fetchImpl?: FetchLike;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}): Promise<{ allowed: boolean; reason: string }> {
  const log = safeLogger(params.logger);
  const now = params.now ?? (() => Date.now());
  const sleep = params.sleep ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const expiresAt = Date.parse(params.approval.expires_at);
  const hardDeadline = now() + params.config.approvalMaxWaitMs;
  const deadline = Number.isFinite(expiresAt) ? Math.min(expiresAt, hardDeadline) : hardDeadline;

  while (now() <= deadline) {
    let status: ApprovalStatusResponse;
    try {
      status = await requestJson<ApprovalStatusResponse>({
        path: `/api/v1/approvals/${params.approval.approval_id}`,
        method: "GET",
        config: params.config,
        fetchImpl: params.fetchImpl,
      });
    } catch (error) {
      log.warn(`IronAgent approval polling failed: ${String(error)}`);
      return {
        allowed: false,
        reason: "IronAgent approval status unavailable",
      };
    }

    if (status.resolution === "allow" || status.status === "approved") {
      return {
        allowed: true,
        reason: "approved by IronAgent",
      };
    }
    if (status.resolution === "deny" || status.status === "denied") {
      return {
        allowed: false,
        reason: "denied in IronAgent",
      };
    }
    await sleep(params.config.approvalPollMs);
  }

  return {
    allowed: false,
    reason: "IronAgent approval timed out",
  };
}

export function resolveIronAgentBridgeConfig(
  env: Record<string, string | undefined> = process.env,
): IronAgentBridgeConfig {
  return {
    enabled: env.IRONAGENT_HOOKS_ENABLED === "true",
    baseUrl: normalizeBaseUrl(trimToUndefined(env.IRONAGENT_SERVICE_URL) ?? "http://localhost:8000"),
    timeoutMs: parsePositiveInt(env.IRONAGENT_HOOK_TIMEOUT_MS, 5000),
    failClosed: env.IRONAGENT_FAIL_CLOSED !== "false",
    apiToken: trimToUndefined(env.IRONAGENT_API_TOKEN),
    assistantId: trimToUndefined(env.IRONAGENT_ASSISTANT_ID),
    assistantName: trimToUndefined(env.IRONAGENT_ASSISTANT_NAME),
    capabilityProfile: trimToUndefined(env.IRONAGENT_CAPABILITY_PROFILE) ?? "onlyread",
    approvalPollMs: parsePositiveInt(env.IRONAGENT_APPROVAL_POLL_MS, 1000),
    approvalMaxWaitMs: parsePositiveInt(env.IRONAGENT_APPROVAL_MAX_WAIT_MS, 300000),
  };
}

export async function handleBeforePromptBuild(
  _event: { prompt: string; messages: unknown[] },
  ctx: AgentHookContextLike,
  options: HandlerOptions,
): Promise<BeforePromptBuildHookResult | void> {
  if (!options.config.enabled) {
    return;
  }

  const log = safeLogger(options.logger);
  const assistant = await fetchAssistantRecord({
    ctx,
    config: options.config,
    fetchImpl: options.fetchImpl,
  });
  const capabilityProfile = assistant?.capability_profile?.trim();
  if (!capabilityProfile) {
    log.warn("IronAgent assistant profile unavailable during before_prompt_build");
    return;
  }

  return {
    prependContext: buildCapabilityPromptHint(capabilityProfile),
  };
}

export async function handleBeforeToolCall(
  event: BeforeToolCallEventLike,
  ctx: ToolHookContextLike,
  options: HandlerOptions,
): Promise<BeforeToolCallHookResult | void> {
  if (!options.config.enabled) {
    return;
  }

  const log = safeLogger(options.logger);
  const now = options.now ?? (() => Date.now());
  const rememberedDenial = consumeRememberedDeniedAttempt({
    runId: event.runId ?? ctx.runId,
    sessionKey: ctx.sessionKey,
    agentId: ctx.agentId,
    toolName: event.toolName,
    toolArgs: event.params,
    now,
  });
  if (rememberedDenial) {
    return {
      block: true,
      blockReason: `${rememberedDenial.reason}. Do not retry the same action in this run.`,
    };
  }
  const request = buildPreExecuteRequest(event, ctx, options.config);
  let response: PreExecuteHookResponse;
  try {
    response = await requestJson<PreExecuteHookResponse>({
      path: "/api/v1/hooks/pre-execute",
      method: "POST",
      config: options.config,
      body: request,
      fetchImpl: options.fetchImpl,
    });
  } catch (error) {
    if (!options.config.failClosed) {
      log.warn(`IronAgent pre-execute unavailable, allowing tool call: ${String(error)}`);
      return;
    }
    return {
      block: true,
      blockReason: `IronAgent unavailable: ${String(error)}`,
    };
  }

  if (response.decision === "allow") {
    rememberDecision(
      { runId: event.runId ?? ctx.runId, toolCallId: event.toolCallId ?? ctx.toolCallId },
      {
        requestId: response.request_id,
        decision: response.decision,
        capabilityProfile: response.capability_profile,
        metadata: response.metadata,
      },
    );
    return isRecord(response.tool_args) ? { params: response.tool_args } : undefined;
  }

  if (response.decision === "deny") {
    rememberDeniedAttempt({
      runId: event.runId ?? ctx.runId,
      sessionKey: ctx.sessionKey,
      agentId: ctx.agentId,
      toolName: event.toolName,
      toolArgs: event.params,
      reason: response.reason,
      expiresAtMs: now() + options.config.approvalMaxWaitMs,
      now,
    });
    await reportBlockedAttempt({
      config: options.config,
      logger: options.logger,
      fetchImpl: options.fetchImpl,
      event,
      ctx,
      requestId: response.request_id,
      capabilityProfile: response.capability_profile,
      reason: response.reason,
      decisionMetadata: response.metadata,
    });
    return {
      block: true,
      blockReason: response.reason,
    };
  }

  if (!response.approval) {
    await reportBlockedAttempt({
      config: options.config,
      logger: options.logger,
      fetchImpl: options.fetchImpl,
      event,
      ctx,
      requestId: response.request_id,
      capabilityProfile: response.capability_profile,
      reason: "IronAgent approval response missing approval id",
      decisionMetadata: response.metadata,
    });
    return {
      block: true,
      blockReason: "IronAgent approval response missing approval id",
    };
  }

  // Check if this is a user approval request - return requireApproval to trigger IronAgent UI
  const metadata = response.metadata ?? {};
  const approvalScope = metadata.approvalScope ?? metadata.approval_scope;
  if (approvalScope === "user") {
    // For user approvals, return requireApproval to show the IronAgent approval UI
    return {
      requireApproval: {
        title: "IronAgent Security Approval",
        description: response.reason || "This action requires your approval",
        severity: "warning",
        timeoutMs: 300000, // 5 minutes
        timeoutBehavior: "deny",
      },
    };
  }

  // For non-user approvals (admin, etc.), poll for the decision
  const approval = await waitForApprovalResolution({
    approval: response.approval,
    config: options.config,
    logger: options.logger,
    fetchImpl: options.fetchImpl,
    sleep: options.sleep,
    now: options.now,
  });
  if (!approval.allowed) {
    rememberDeniedAttempt({
      runId: event.runId ?? ctx.runId,
      sessionKey: ctx.sessionKey,
      agentId: ctx.agentId,
      toolName: event.toolName,
      toolArgs: event.params,
      reason: approval.reason,
      expiresAtMs: now() + options.config.approvalMaxWaitMs,
      now,
    });
    await reportBlockedAttempt({
      config: options.config,
      logger: options.logger,
      fetchImpl: options.fetchImpl,
      event,
      ctx,
      requestId: response.request_id,
      capabilityProfile: response.capability_profile,
      reason: approval.reason,
      decisionMetadata: response.metadata,
    });
    return {
      block: true,
      blockReason: approval.reason,
    };
  }

  rememberDecision(
    { runId: event.runId ?? ctx.runId, toolCallId: event.toolCallId ?? ctx.toolCallId },
    {
      requestId: response.request_id,
      decision: "allow",
      capabilityProfile: response.capability_profile,
      metadata: response.metadata,
    },
  );
  return isRecord(response.tool_args) ? { params: response.tool_args } : undefined;
}

export async function handleAfterToolCall(
  event: AfterToolCallEventLike,
  ctx: ToolHookContextLike,
  options: HandlerOptions,
): Promise<void> {
  if (!options.config.enabled) {
    return;
  }

  const log = safeLogger(options.logger);
  const storedDecision = consumeDecision({
    runId: event.runId ?? ctx.runId,
    toolCallId: event.toolCallId ?? ctx.toolCallId,
  });

  try {
    await requestJson({
      path: "/api/v1/hooks/post-execute",
      method: "POST",
      config: options.config,
      fetchImpl: options.fetchImpl,
      body: buildPostExecuteRequest({
        event,
        ctx,
        config: options.config,
        requestId: storedDecision?.requestId,
        decision: storedDecision?.decision,
        capabilityProfile: storedDecision?.capabilityProfile,
        decisionMetadata: storedDecision?.metadata,
        success: !event.error,
        error: event.error,
      }),
    });
  } catch (error) {
    log.warn(`IronAgent post-execute failed: ${String(error)}`);
  }
}

export const __testing = {
  buildPreExecuteRequest,
  buildPostExecuteRequest,
  buildDeniedAttemptKey,
  buildDecisionKey,
  consumeRememberedDeniedAttempt,
  consumeDecision,
  inferRisk,
  rememberDeniedAttempt,
  rememberedDeniedAttempts,
  rememberDecision,
  summarizeToolResult,
  storedDecisions,
  waitForApprovalResolution,
};
