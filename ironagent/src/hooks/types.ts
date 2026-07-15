export const HOOK_PROTOCOL_VERSION = "2026-04-03" as const;

export type HookName = "pre_execute" | "post_execute";
export type HookDecision = "allow" | "deny" | "pending_approval";
export type RiskLevel = "low" | "medium" | "high" | "critical";
export type ToolSource = "core" | "plugin" | "channel" | "client";

export type HookEnvelope = {
  protocol_version: typeof HOOK_PROTOCOL_VERSION;
  request_id: string;
  source: "ironagent";
  hook_name: HookName;
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
