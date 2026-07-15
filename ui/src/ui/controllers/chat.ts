import { parseAgentSessionKey } from "../../../../src/routing/session-key.js";
import { resetToolStream } from "../app-tool-stream.ts";
import { extractText } from "../chat/message-extract.ts";
import { formatConnectError } from "../connect-error.ts";
import type { GatewayBrowserClient } from "../gateway.ts";
import type { AgentsFilesListResult } from "../types.ts";
import type { ChatAttachment } from "../ui-types.ts";
import { generateUUID } from "../uuid.ts";
import {
  formatMissingOperatorReadScopeMessage,
  isMissingOperatorReadScopeError,
} from "./scope-errors.ts";
import {
  uploadWorkspaceFiles,
  loadWorkspaceFiles,
  type WorkspaceFilesState,
} from "./workspace-files.ts";

const SILENT_REPLY_PATTERN = /^\s*NO_REPLY\s*$/;
const TRANSIENT_CHAT_HISTORY_STARTUP_MESSAGE = "chat.history unavailable during gateway startup";
const CHAT_RUN_FALLBACK_DELAY_MS = 8_000;
const CHAT_RUN_FALLBACK_WAIT_MS = 1_500;

function isSilentReplyStream(text: string): boolean {
  return SILENT_REPLY_PATTERN.test(text);
}

function isTransientChatHistoryStartupError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return message.includes(TRANSIENT_CHAT_HISTORY_STARTUP_MESSAGE);
}

function scheduleChatHistoryRetry(state: ChatState) {
  if (typeof globalThis.setTimeout !== "function") {
    return;
  }
  globalThis.setTimeout(() => {
    if (!state.connected || state.chatLoading) {
      return;
    }
    void loadChatHistory(state);
  }, 750);
}

function scheduleChatRunFallbackRefresh(state: ChatState, runId: string) {
  if (typeof globalThis.setTimeout !== "function") {
    return;
  }
  globalThis.setTimeout(() => {
    void refreshCompletedChatRun(state, runId);
  }, CHAT_RUN_FALLBACK_DELAY_MS);
}

function refreshChatWorkspaceFiles(state: ChatState) {
  const parsed = parseAgentSessionKey(state.sessionKey);
  const assistantId = parsed?.agentId ?? "main";
  void loadWorkspaceFiles(state as unknown as WorkspaceFilesState, assistantId, {
    background: true,
    sync: true,
  });
}

async function refreshCompletedChatRun(state: ChatState, runId: string) {
  if (!state.client || !state.connected || state.chatRunId !== runId) {
    return;
  }

  let status: unknown;
  try {
    const res = await state.client.request<{ status?: unknown }>("agent.wait", {
      runId,
      timeoutMs: CHAT_RUN_FALLBACK_WAIT_MS,
    });
    status = res.status;
  } catch {
    return;
  }

  if (state.chatRunId !== runId) {
    return;
  }
  if (status === "ok" || status === "error") {
    try {
      await loadChatHistory(state);
    } finally {
      refreshChatWorkspaceFiles(state);
      if (state.chatRunId === runId) {
        state.chatRunId = null;
        state.chatStream = null;
        state.chatStreamStartedAt = null;
      }
    }
    return;
  }
  if (status === "timeout") {
    scheduleChatRunFallbackRefresh(state, runId);
  }
}
/** Client-side defense-in-depth: detect assistant messages whose text is purely NO_REPLY. */
function isAssistantSilentReply(message: unknown): boolean {
  if (!message || typeof message !== "object") {
    return false;
  }
  const entry = message as Record<string, unknown>;
  const role = typeof entry.role === "string" ? entry.role.toLowerCase() : "";
  if (role !== "assistant") {
    return false;
  }
  // entry.text takes precedence — matches gateway extractAssistantTextForSilentCheck
  if (typeof entry.text === "string") {
    return isSilentReplyStream(entry.text);
  }
  const text = extractText(message);
  return typeof text === "string" && isSilentReplyStream(text);
}

export type ChatState = {
  client: GatewayBrowserClient | null;
  connected: boolean;
  sessionKey: string;
  chatLoading: boolean;
  chatMessages: unknown[];
  chatThinkingLevel: string | null;
  chatSending: boolean;
  chatMessage: string;
  chatAttachments: ChatAttachment[];
  chatRunId: string | null;
  chatStream: string | null;
  chatStreamStartedAt: number | null;
  lastError: string | null;
  // Workspace-files state (needed by uploadWorkspaceFiles/loadWorkspaceFiles)
  workspaceFilesLoading: boolean;
  workspaceFilesError: string | null;
  workspaceFilesList: AgentsFilesListResult | null;
};

export type ChatEventPayload = {
  runId: string;
  sessionKey: string;
  state: "delta" | "final" | "aborted" | "error";
  message?: unknown;
  errorMessage?: string;
};

function maybeResetToolStream(state: ChatState) {
  const toolHost = state as ChatState & Partial<Parameters<typeof resetToolStream>[0]>;
  if (
    toolHost.toolStreamById instanceof Map &&
    Array.isArray(toolHost.toolStreamOrder) &&
    Array.isArray(toolHost.chatToolMessages) &&
    Array.isArray(toolHost.chatStreamSegments)
  ) {
    resetToolStream(toolHost as Parameters<typeof resetToolStream>[0]);
  }
}

export async function loadChatHistory(state: ChatState) {
  if (!state.client || !state.connected) {
    return;
  }
  state.chatLoading = true;
  state.lastError = null;
  try {
    const res = await state.client.request<{ messages?: Array<unknown>; thinkingLevel?: string }>(
      "chat.history",
      {
        sessionKey: state.sessionKey,
        limit: 200,
      },
    );
    const messages = Array.isArray(res.messages) ? res.messages : [];
    state.chatMessages = messages.filter((message) => !isAssistantSilentReply(message));
    state.chatThinkingLevel = res.thinkingLevel ?? null;
    // Clear all streaming state — history includes tool results and text
    // inline, so keeping streaming artifacts would cause duplicates.
    maybeResetToolStream(state);
    state.chatStream = null;
    state.chatStreamStartedAt = null;
  } catch (err) {
    if (isMissingOperatorReadScopeError(err)) {
      state.chatMessages = [];
      state.chatThinkingLevel = null;
      state.lastError = formatMissingOperatorReadScopeMessage("existing chat history");
    } else if (isTransientChatHistoryStartupError(err)) {
      state.lastError = null;
      scheduleChatHistoryRetry(state);
    } else {
      state.lastError = String(err);
    }
  } finally {
    state.chatLoading = false;
  }
}

type AssistantMessageNormalizationOptions = {
  roleRequirement: "required" | "optional";
  roleCaseSensitive?: boolean;
  requireContentArray?: boolean;
  allowTextField?: boolean;
};

function normalizeAssistantMessage(
  message: unknown,
  options: AssistantMessageNormalizationOptions,
): Record<string, unknown> | null {
  if (!message || typeof message !== "object") {
    return null;
  }
  const candidate = message as Record<string, unknown>;
  const roleValue = candidate.role;
  if (typeof roleValue === "string") {
    const role = options.roleCaseSensitive ? roleValue : roleValue.toLowerCase();
    if (role !== "assistant") {
      return null;
    }
  } else if (options.roleRequirement === "required") {
    return null;
  }

  if (options.requireContentArray) {
    return Array.isArray(candidate.content) ? candidate : null;
  }
  if (!("content" in candidate) && !(options.allowTextField && "text" in candidate)) {
    return null;
  }
  return candidate;
}

function normalizeAbortedAssistantMessage(message: unknown): Record<string, unknown> | null {
  return normalizeAssistantMessage(message, {
    roleRequirement: "required",
    roleCaseSensitive: true,
    requireContentArray: true,
  });
}

function normalizeFinalAssistantMessage(message: unknown): Record<string, unknown> | null {
  return normalizeAssistantMessage(message, {
    roleRequirement: "optional",
    allowTextField: true,
  });
}

export async function sendChatMessage(
  state: ChatState,
  message: string,
  attachments?: ChatAttachment[],
): Promise<string | null> {
  if (!state.client || !state.connected) {
    return null;
  }
  const msg = message.trim();
  const hasAttachments = attachments && attachments.length > 0;
  if (!msg && !hasAttachments) {
    return null;
  }

  const now = Date.now();
  const runId = generateUUID();

  // Step 1: Upload attachments to workspace temp before calling chat.send.
  // All-or-nothing: any upload failure cancels the entire send.
  const uploadedPaths: string[] = [];
  if (hasAttachments) {
    const parsed = parseAgentSessionKey(state.sessionKey);
    const assistantId = parsed?.agentId ?? "main";
    const files: File[] = attachments.map((att) => att.file);
    try {
      const results = await uploadWorkspaceFiles(
        state as unknown as WorkspaceFilesState,
        assistantId,
        files,
        {
          targetBucket: "temp",
          sourceType: "webui-chat",
          sourceRef: `chat-send:${runId}`,
        },
      );
      for (const r of results) {
        if (r.path?.trim()) {
          uploadedPaths.push(r.path);
        }
        if (r.status === "rejected") {
          state.lastError = formatConnectError(r.message ?? "Upload rejected");
          return null;
        }
      }
    } catch (err) {
      state.lastError = formatConnectError(err);
      return null;
    }
    if (uploadedPaths.length !== files.length) {
      state.lastError = formatConnectError("Some files were not uploaded");
      return null;
    }
  }

  // Step 2: Build the final message text with attachment block
  let finalMessage = msg;
  if (uploadedPaths.length > 0) {
    const attachmentLines = uploadedPaths.map((p) => `- ${p}`).join("\n");
    const attachmentBlock = `[附件]\n${attachmentLines}`;
    if (msg) {
      finalMessage = `${msg}\n\n${attachmentBlock}`;
    } else {
      finalMessage = `[用户发送了以下附件]\n\n${attachmentBlock}`;
    }
  }

  // Build local user message display (attachment paths, not the old placeholder)
  const contentBlocks: Array<{ type: string; text?: string }> = [];
  if (msg) {
    contentBlocks.push({ type: "text", text: msg });
  }
  if (uploadedPaths.length > 0) {
    for (const p of uploadedPaths) {
      contentBlocks.push({ type: "text", text: `[附件: ${p}]` });
    }
  }

  state.chatMessages = [
    ...state.chatMessages,
    {
      role: "user",
      content: contentBlocks,
      timestamp: now,
    },
  ];

  state.chatSending = true;
  state.lastError = null;
  state.chatRunId = runId;
  state.chatStream = "";
  state.chatStreamStartedAt = now;

  try {
    await state.client.request("chat.send", {
      sessionKey: state.sessionKey,
      message: finalMessage,
      deliver: false,
      idempotencyKey: runId,
    });
    refreshChatWorkspaceFiles(state);
    scheduleChatRunFallbackRefresh(state, runId);
    return runId;
  } catch (err) {
    const error = formatConnectError(err);
    state.chatRunId = null;
    state.chatStream = null;
    state.chatStreamStartedAt = null;
    state.lastError = error;
    state.chatMessages = [
      ...state.chatMessages,
      {
        role: "assistant",
        content: [{ type: "text", text: "Error: " + error }],
        timestamp: Date.now(),
      },
    ];
    return null;
  } finally {
    state.chatSending = false;
  }
}

export async function abortChatRun(state: ChatState): Promise<boolean> {
  if (!state.client || !state.connected) {
    return false;
  }
  const runId = state.chatRunId;
  try {
    await state.client.request(
      "chat.abort",
      runId ? { sessionKey: state.sessionKey, runId } : { sessionKey: state.sessionKey },
    );
    return true;
  } catch (err) {
    state.lastError = formatConnectError(err);
    return false;
  }
}

export function handleChatEvent(state: ChatState, payload?: ChatEventPayload) {
  if (!payload) {
    return null;
  }
  if (payload.sessionKey !== state.sessionKey) {
    return null;
  }

  // Final from another run (e.g. sub-agent announce): refresh history to show new message.
  // See https://github.com/aiujdha/ironagent/issues
  if (payload.runId && state.chatRunId && payload.runId !== state.chatRunId) {
    if (payload.state === "final") {
      refreshChatWorkspaceFiles(state);
      const finalMessage = normalizeFinalAssistantMessage(payload.message);
      if (finalMessage && !isAssistantSilentReply(finalMessage)) {
        state.chatMessages = [...state.chatMessages, finalMessage];
        return null;
      }
      return "final";
    }
    return null;
  }

  if (payload.state === "delta") {
    const next = extractText(payload.message);
    if (typeof next === "string" && !isSilentReplyStream(next)) {
      state.chatStream = next;
    }
  } else if (payload.state === "final") {
    const finalMessage = normalizeFinalAssistantMessage(payload.message);
    if (finalMessage && !isAssistantSilentReply(finalMessage)) {
      state.chatMessages = [...state.chatMessages, finalMessage];
    } else if (state.chatStream?.trim() && !isSilentReplyStream(state.chatStream)) {
      state.chatMessages = [
        ...state.chatMessages,
        {
          role: "assistant",
          content: [{ type: "text", text: state.chatStream }],
          timestamp: Date.now(),
        },
      ];
    }
    state.chatStream = null;
    state.chatRunId = null;
    state.chatStreamStartedAt = null;
    refreshChatWorkspaceFiles(state);
  } else if (payload.state === "aborted") {
    const normalizedMessage = normalizeAbortedAssistantMessage(payload.message);
    if (normalizedMessage && !isAssistantSilentReply(normalizedMessage)) {
      state.chatMessages = [...state.chatMessages, normalizedMessage];
    } else {
      const streamedText = state.chatStream ?? "";
      if (streamedText.trim() && !isSilentReplyStream(streamedText)) {
        state.chatMessages = [
          ...state.chatMessages,
          {
            role: "assistant",
            content: [{ type: "text", text: streamedText }],
            timestamp: Date.now(),
          },
        ];
      }
    }
    state.chatStream = null;
    state.chatRunId = null;
    state.chatStreamStartedAt = null;
    refreshChatWorkspaceFiles(state);
  } else if (payload.state === "error") {
    state.chatStream = null;
    state.chatRunId = null;
    state.chatStreamStartedAt = null;
    state.lastError = payload.errorMessage ?? "chat error";
    refreshChatWorkspaceFiles(state);
  }
  return payload.state;
}
