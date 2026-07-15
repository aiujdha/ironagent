import { HOOK_PROTOCOL_VERSION } from "./types.js";

export type HookConfig = {
  baseUrl: string;
  timeoutMs: number;
  protocolVersion: typeof HOOK_PROTOCOL_VERSION;
  failClosed: boolean;
  apiToken?: string;
};

export function resolveHookConfig(env: Record<string, string | undefined> = process.env): HookConfig {
  const timeoutRaw = Number(env.IRON_AGENT_HOOK_TIMEOUT_MS ?? "5000");

  return {
    baseUrl: env.IRON_AGENT_SERVICE_URL ?? "http://localhost:8000",
    timeoutMs: Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? timeoutRaw : 5000,
    protocolVersion: HOOK_PROTOCOL_VERSION,
    failClosed: env.IRON_AGENT_FAIL_CLOSED !== "false",
    apiToken: env.IRON_AGENT_API_TOKEN,
  };
}
