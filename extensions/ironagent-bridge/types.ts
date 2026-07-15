export const HOOK_PROTOCOL_VERSION = "2026-04-03" as const;

export type HookDecision = "allow" | "deny" | "pending_approval";
export type RiskLevel = "low" | "medium" | "high" | "critical";
export type ToolSource = "core" | "plugin" | "channel" | "client";

export type HookEnvelope = {
  protocol_version: typeof HOOK_PROTOCOL_VERSION;
  request_id: string;
  source: "ironagent";
  hook_name: "pre_execute" | "post_execute";
  occurred_at: string;
};

export type AssistantContext = {
  assistant_id?: string;
  name?: string;
  capability_profile: string;
};

export type SessionContext = {
  session_id?: string;
  run_id?: string;
  conversation_id?: string;
  channel?: string;
  user_id?: string;
};

export type ToolContext = {
  name: string;
  args: Record<string, unknown>;
  source?: ToolSource;
  optional?: boolean;
  owner_only?: boolean;
};

export type RiskContext = {
  level: RiskLevel;
  reason?: string;
  tags: string[];
};

export type ApprovalContext = {
  required: true;
  approval_id: string;
  status: "pending";
  expires_at: string;
};

export type PreExecuteHookRequest = {
  envelope: HookEnvelope;
  assistant: AssistantContext;
  session: SessionContext;
  tool: ToolContext;
  risk: RiskContext;
  metadata: Record<string, unknown>;
};

export type PreExecuteHookResponse = {
  protocol_version: typeof HOOK_PROTOCOL_VERSION;
  request_id: string;
  decision: HookDecision;
  reason: string;
  capability_profile: string;
  fail_closed: boolean;
  approval?: ApprovalContext;
  metadata: Record<string, unknown>;
  tool_args?: Record<string, unknown> | null;
};

export type ExecutionResultContext = {
  success: boolean;
  duration_ms?: number;
  error?: string;
  output_summary: Record<string, unknown>;
};

export type PostExecuteHookRequest = {
  envelope: HookEnvelope;
  assistant: AssistantContext;
  session: SessionContext;
  tool: ToolContext;
  result: ExecutionResultContext;
  decision?: HookDecision;
  metadata: Record<string, unknown>;
};

export type PostExecuteHookResponse = {
  protocol_version: typeof HOOK_PROTOCOL_VERSION;
  request_id: string;
  status: "ok";
};

export type ApprovalStatusResponse = {
  approval_id: string;
  tool_name: string;
  assistant_id?: string;
  capability_profile?: string;
  status: string;
  resolution?: "allow" | "deny" | null;
  requested_at: string;
  expires_at: string;
  resolved_at?: string | null;
};

export type AssistantRecordResponse = {
  assistant_id: string;
  name: string;
  capability_profile: string;
  notes?: string;
  created_at?: string;
  updated_at?: string;
};

export type BeforeToolCallEventLike = {
  toolName: string;
  params: Record<string, unknown>;
  runId?: string;
  toolCallId?: string;
};

export type AfterToolCallEventLike = {
  toolName: string;
  params: Record<string, unknown>;
  runId?: string;
  toolCallId?: string;
  result?: unknown;
  error?: string;
  durationMs?: number;
};

export type ToolHookContextLike = {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  runId?: string;
  toolName: string;
  toolCallId?: string;
};

export type AgentHookContextLike = {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  runId?: string;
  workspaceDir?: string;
  messageProvider?: string;
  trigger?: string;
  channelId?: string;
};

export type BeforePromptBuildHookResult = {
  prependContext?: string;
};

export type BeforeToolCallHookResult = {
  block?: boolean;
  blockReason?: string;
  params?: Record<string, unknown>;
  requireApproval?: {
    title: string;
    description: string;
    severity?: "info" | "warning" | "critical";
    timeoutMs?: number;
    timeoutBehavior?: "allow" | "deny";
  };
};

export type IronAgentBridgeConfig = {
  enabled: boolean;
  baseUrl: string;
  timeoutMs: number;
  failClosed: boolean;
  apiToken?: string;
  assistantId?: string;
  assistantName?: string;
  capabilityProfile: string;
  approvalPollMs: number;
  approvalMaxWaitMs: number;
};

export type IronAgentBridgeLogger = {
  debug?: (message: string) => void;
  info?: (message: string) => void;
  warn?: (message: string) => void;
  error?: (message: string) => void;
};
