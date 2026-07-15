import type { MsgContext } from "../../auto-reply/templating.js";

// Phase 10.2: discord plugin removed; no channel-specific session-key normalizers remain.
export function normalizeExplicitSessionKey(sessionKey: string, ctx: MsgContext): string {
  void ctx;
  return sessionKey.trim().toLowerCase();
}
