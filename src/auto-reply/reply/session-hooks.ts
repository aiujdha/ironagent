import { resolveSessionAgentId } from "../../agents/agent-scope.js";
import type { IronAgentConfig } from "../../config/config.js";

export type SessionHookContext = {
  sessionId: string;
  sessionKey: string;
  agentId: string;
};

function buildSessionHookContext(params: {
  sessionId: string;
  sessionKey: string;
  cfg: IronAgentConfig;
}): SessionHookContext {
  return {
    sessionId: params.sessionId,
    sessionKey: params.sessionKey,
    agentId: resolveSessionAgentId({ sessionKey: params.sessionKey, config: params.cfg }),
  };
}

export function buildSessionStartHookPayload(params: {
  sessionId: string;
  sessionKey: string;
  cfg: IronAgentConfig;
  resumedFrom?: string;
}): {
  event: { sessionId: string; sessionKey: string; resumedFrom?: string };
  context: SessionHookContext;
} {
  return {
    event: {
      sessionId: params.sessionId,
      sessionKey: params.sessionKey,
      resumedFrom: params.resumedFrom,
    },
    context: buildSessionHookContext({
      sessionId: params.sessionId,
      sessionKey: params.sessionKey,
      cfg: params.cfg,
    }),
  };
}

export function buildSessionEndHookPayload(params: {
  sessionId: string;
  sessionKey: string;
  cfg: IronAgentConfig;
  messageCount?: number;
}): {
  event: { sessionId: string; sessionKey: string; messageCount: number };
  context: SessionHookContext;
} {
  return {
    event: {
      sessionId: params.sessionId,
      sessionKey: params.sessionKey,
      messageCount: params.messageCount ?? 0,
    },
    context: buildSessionHookContext({
      sessionId: params.sessionId,
      sessionKey: params.sessionKey,
      cfg: params.cfg,
    }),
  };
}
