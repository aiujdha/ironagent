/* @vitest-environment jsdom */

import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatHost } from "./app-chat.ts";

const { loadWorkspaceFilesMock, setLastActiveSessionKeyMock, uploadWorkspaceFilesMock } =
  vi.hoisted(() => ({
    loadWorkspaceFilesMock: vi.fn(),
    setLastActiveSessionKeyMock: vi.fn(),
    uploadWorkspaceFilesMock: vi.fn(),
  }));

vi.mock("./app-settings.ts", () => ({
  setLastActiveSessionKey: (...args: unknown[]) => setLastActiveSessionKeyMock(...args),
}));

vi.mock("./controllers/workspace-files.ts", () => ({
  loadWorkspaceFiles: (...args: unknown[]) => loadWorkspaceFilesMock(...args),
  uploadWorkspaceFiles: (...args: unknown[]) => uploadWorkspaceFilesMock(...args),
}));

let handleSendChat: typeof import("./app-chat.ts").handleSendChat;
let refreshChatAvatar: typeof import("./app-chat.ts").refreshChatAvatar;
let clearPendingQueueItemsForRun: typeof import("./app-chat.ts").clearPendingQueueItemsForRun;

async function loadChatHelpers(): Promise<void> {
  vi.resetModules();
  ({ handleSendChat, refreshChatAvatar, clearPendingQueueItemsForRun } =
    await import("./app-chat.ts"));
}

function makeHost(overrides?: Partial<ChatHost>): ChatHost {
  return {
    client: null,
    chatMessages: [],
    chatToolMessages: [],
    chatStreamSegments: [],
    chatStream: null,
    toolStreamById: new Map(),
    toolStreamOrder: [],
    toolStreamSyncTimer: null,
    connected: true,
    chatMessage: "",
    chatAttachments: [],
    chatQueue: [],
    chatRunId: null,
    chatSending: false,
    lastError: null,
    sessionKey: "agent:main",
    basePath: "",
    hello: null,
    chatAvatarUrl: null,
    chatModelOverrides: {},
    chatModelsLoading: false,
    chatModelCatalog: [],
    refreshSessionsAfterChat: new Set<string>(),
    workspaceFilesLoading: false,
    workspaceFilesError: null,
    workspaceFilesList: null,
    updateComplete: Promise.resolve(),
    ...overrides,
  } as ChatHost;
}

describe("refreshChatAvatar", () => {
  beforeEach(async () => {
    await loadChatHelpers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses a route-relative avatar endpoint before basePath bootstrap finishes", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ avatarUrl: "/avatar/main" }),
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const host = makeHost({ basePath: "", sessionKey: "agent:main" });
    await refreshChatAvatar(host);

    expect(fetchMock).toHaveBeenCalledWith(
      "avatar/main?meta=1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(host.chatAvatarUrl).toBe("/avatar/main");
  });

  it("keeps mounted dashboard avatar endpoints under the normalized base path", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const host = makeHost({ basePath: "/ironagent/", sessionKey: "agent:ops:main" });
    await refreshChatAvatar(host);

    expect(fetchMock).toHaveBeenCalledWith(
      "/ironagent/avatar/ops?meta=1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(host.chatAvatarUrl).toBeNull();
  });
});

describe("handleSendChat", () => {
  beforeEach(async () => {
    loadWorkspaceFilesMock.mockReset();
    setLastActiveSessionKeyMock.mockReset();
    uploadWorkspaceFilesMock.mockReset();
    await loadChatHelpers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.doUnmock("./chat/slash-command-executor.ts");
  });

  it("keeps slash-command model changes in sync with the chat header cache", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({}),
      }) as unknown as typeof fetch,
    );
    const request = vi.fn(async (method: string, _params?: unknown) => {
      if (method === "sessions.patch") {
        return {
          ok: true,
          key: "main",
          resolved: {
            modelProvider: "openai",
            model: "gpt-5-mini",
          },
        };
      }
      if (method === "chat.history") {
        return { messages: [], thinkingLevel: null };
      }
      if (method === "sessions.list") {
        return {
          ts: 0,
          path: "",
          count: 0,
          defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: null },
          sessions: [],
        };
      }
      if (method === "models.list") {
        return {
          models: [{ id: "gpt-5-mini", name: "GPT-5 Mini", provider: "openai" }],
        };
      }
      throw new Error(`Unexpected request: ${method}`);
    });
    const onSlashAction = vi.fn();
    const host = makeHost({
      client: { request } as unknown as ChatHost["client"],
      sessionKey: "main",
      chatMessage: "/model gpt-5-mini",
      onSlashAction,
    });

    await handleSendChat(host);

    expect(request).toHaveBeenCalledWith("sessions.patch", {
      key: "main",
      model: "gpt-5-mini",
    });
    expect(host.chatModelOverrides.main).toEqual({
      kind: "qualified",
      value: "openai/gpt-5-mini",
    });
    expect(onSlashAction).toHaveBeenCalledWith("refresh-tools-effective");
  });

  it("shows a visible pending item for /steer on the active run", async () => {
    vi.doMock("./chat/slash-command-executor.ts", async () => {
      const actual = await vi.importActual<typeof import("./chat/slash-command-executor.ts")>(
        "./chat/slash-command-executor.ts",
      );
      return {
        ...actual,
        executeSlashCommand: vi.fn(async () => ({
          content: "Steered.",
          pendingCurrentRun: true,
        })),
      };
    });
    await loadChatHelpers();

    const host = makeHost({
      client: { request: vi.fn() } as unknown as ChatHost["client"],
      chatRunId: "run-1",
      chatMessage: "/steer tighten the plan",
    });

    await handleSendChat(host);

    expect(host.chatQueue).toEqual([
      expect.objectContaining({
        text: "/steer tighten the plan",
        pendingRunId: "run-1",
      }),
    ]);
  });

  it("removes pending steer indicators when the run finishes", async () => {
    const host = makeHost({
      chatQueue: [
        {
          id: "pending",
          text: "/steer tighten the plan",
          createdAt: 1,
          pendingRunId: "run-1",
        },
        {
          id: "queued",
          text: "follow up",
          createdAt: 2,
        },
      ],
    });

    clearPendingQueueItemsForRun(host, "run-1");

    expect(host.chatQueue).toEqual([
      expect.objectContaining({
        id: "queued",
        text: "follow up",
      }),
    ]);
  });

  it("uploads pending files to workspace temp before sending chat message", async () => {
    uploadWorkspaceFilesMock.mockResolvedValue([
      {
        status: "accepted",
        fileId: "file-1",
        assistantId: "main",
        filename: "report.pdf",
        bucket: "temp",
        path: "temp/report.pdf",
      },
    ]);
    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.send") {
        return { runId: "run-1", status: "started" };
      }
      throw new Error(`Unexpected request: ${method} ${JSON.stringify(params)}`);
    });
    const host = makeHost({
      client: { request } as unknown as ChatHost["client"],
      sessionKey: "agent:main",
      chatMessage: "Please summarize this file",
      chatAttachments: [
        {
          id: "att-1",
          file: new File(["pdf"], "report.pdf", { type: "application/pdf" }),
          name: "report.pdf",
          mimeType: "application/pdf",
          size: 3,
          previewUrl: null,
        },
      ],
    });

    await handleSendChat(host);

    expect(uploadWorkspaceFilesMock).toHaveBeenCalledWith(
      expect.anything(),
      "main",
      [expect.any(File)],
      expect.objectContaining({
        targetBucket: "temp",
        sourceType: "webui-chat",
        sourceRef: expect.stringMatching(/^chat-send:/),
      }),
    );
    expect(request).toHaveBeenCalledWith(
      "chat.send",
      expect.objectContaining({
        message: "Please summarize this file\n\n[附件]\n- temp/report.pdf",
      }),
    );
    expect(host.chatMessages.at(-1)).toEqual(
      expect.objectContaining({
        role: "user",
        content: expect.arrayContaining([
          expect.objectContaining({ text: "[附件: temp/report.pdf]" }),
        ]),
      }),
    );
    expect(host.chatAttachments).toEqual([]);
    expect(loadWorkspaceFilesMock).toHaveBeenCalledWith(expect.anything(), "main", {
      background: true,
      sync: true,
    });
  });

  it("restores draft and attachments when sending a message with attachments fails", async () => {
    uploadWorkspaceFilesMock.mockResolvedValue([
      {
        status: "accepted",
        fileId: "file-1",
        assistantId: "main",
        filename: "report.pdf",
        bucket: "temp",
        path: "temp/report.pdf",
      },
    ]);
    const request = vi.fn().mockRejectedValue(new Error("send failed"));
    const attachment = {
      id: "att-1",
      file: new File(["pdf"], "report.pdf", { type: "application/pdf" }),
      name: "report.pdf",
      mimeType: "application/pdf",
      size: 3,
      previewUrl: null,
    };
    const host = makeHost({
      client: { request } as unknown as ChatHost["client"],
      chatMessage: "Please summarize this file",
      chatAttachments: [attachment],
    });

    await handleSendChat(host);

    expect(uploadWorkspaceFilesMock).toHaveBeenCalledWith(
      expect.anything(),
      "main",
      [expect.any(File)],
      expect.objectContaining({
        targetBucket: "temp",
        sourceType: "webui-chat",
        sourceRef: expect.stringMatching(/^chat-send:/),
      }),
    );
    expect(request).toHaveBeenCalledWith(
      "chat.send",
      expect.objectContaining({
        message: "Please summarize this file\n\n[附件]\n- temp/report.pdf",
      }),
    );
    expect(host.chatMessage).toBe("Please summarize this file");
    expect(host.chatAttachments).toEqual([attachment]);
  });
});

afterAll(() => {
  vi.doUnmock("./app-settings.ts");
  vi.resetModules();
});
