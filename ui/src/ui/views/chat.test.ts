/* @vitest-environment jsdom */

import { html, render } from "lit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { i18n } from "../../i18n/index.ts";
import { getSafeLocalStorage } from "../../local-storage.ts";
import { renderChatSessionSelect } from "../app-render.helpers.ts";
import { filterExecApprovalsForChat } from "../app-render.ts";
import type { AppViewState } from "../app-view-state.ts";
import {
  createModelCatalog,
  createSessionsListResult,
  DEEPSEEK_CHAT_MODEL,
  DEFAULT_CHAT_MODEL_CATALOG,
} from "../chat-model.test-helpers.ts";
import { SKIP_DELETE_CONFIRM_KEY } from "../chat/grouped-render.ts";
import { formatDisplayPath } from "../display-paths.ts";
import type { GatewayBrowserClient } from "../gateway.ts";
import type { ModelCatalogEntry, SkillStatusEntry } from "../types.ts";
import type { SessionsListResult } from "../types.ts";
import { renderAgentOverview } from "./agents-panels-overview.ts";
import { renderAgentFiles } from "./agents-panels-status-files.ts";
import { renderChat, resetChatViewState, type ChatProps } from "./chat.ts";
import { renderExecApprovalPrompt } from "./exec-approval.ts";
import { renderOverview, type OverviewProps } from "./overview.ts";
import { renderSkills } from "./skills.ts";

function readDeleteConfirmPreference(): string | null {
  try {
    return getSafeLocalStorage()?.getItem(SKIP_DELETE_CONFIRM_KEY) ?? null;
  } catch {
    return null;
  }
}

function clearDeleteConfirmPreference(): void {
  try {
    getSafeLocalStorage()?.removeItem(SKIP_DELETE_CONFIRM_KEY);
  } catch {
    /* noop */
  }
}

function restoreDeleteConfirmPreference(value: string | null): void {
  try {
    if (value === null) {
      getSafeLocalStorage()?.removeItem(SKIP_DELETE_CONFIRM_KEY);
      return;
    }
    getSafeLocalStorage()?.setItem(SKIP_DELETE_CONFIRM_KEY, value);
  } catch {
    /* noop */
  }
}

function createSessions(): SessionsListResult {
  return {
    ts: 0,
    path: "",
    count: 0,
    defaults: { modelProvider: null, model: null, contextTokens: null },
    sessions: [],
  };
}

function createChatHeaderState(
  overrides: {
    model?: string | null;
    modelProvider?: string | null;
    models?: ModelCatalogEntry[];
    omitSessionFromList?: boolean;
  } = {},
): { state: AppViewState; request: ReturnType<typeof vi.fn> } {
  let currentModel = overrides.model ?? null;
  let currentModelProvider = overrides.modelProvider ?? (currentModel ? "openai" : null);
  const omitSessionFromList = overrides.omitSessionFromList ?? false;
  const catalog = overrides.models ?? createModelCatalog(...DEFAULT_CHAT_MODEL_CATALOG);
  const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
    if (method === "sessions.patch") {
      const nextModel = (params.model as string | null | undefined) ?? null;
      if (!nextModel) {
        currentModel = null;
        currentModelProvider = null;
      } else {
        const normalized = nextModel.trim();
        const slashIndex = normalized.indexOf("/");
        if (slashIndex > 0) {
          currentModelProvider = normalized.slice(0, slashIndex);
          currentModel = normalized.slice(slashIndex + 1);
        } else {
          currentModel = normalized;
          const matchingProviders = catalog
            .filter((entry) => entry.id === normalized)
            .map((entry) => entry.provider)
            .filter(Boolean);
          currentModelProvider =
            matchingProviders.length === 1 ? matchingProviders[0] : currentModelProvider;
        }
      }
      return { ok: true, key: "main" };
    }
    if (method === "chat.history") {
      return { messages: [], thinkingLevel: null };
    }
    if (method === "sessions.list") {
      return createSessionsListResult({
        model: currentModel,
        modelProvider: currentModelProvider,
        omitSessionFromList,
      });
    }
    if (method === "models.list") {
      return { models: catalog };
    }
    if (method === "tools.effective") {
      return {
        agentId: "main",
        profile: "coding",
        groups: [],
      };
    }
    throw new Error(`Unexpected request: ${method}`);
  });
  const state = {
    sessionKey: "main",
    connected: true,
    sessionsHideCron: true,
    sessionsResult: createSessionsListResult({
      model: currentModel,
      modelProvider: currentModelProvider,
      omitSessionFromList,
    }),
    chatModelOverrides: {},
    chatModelCatalog: catalog,
    chatModelsLoading: false,
    client: { request } as unknown as GatewayBrowserClient,
    settings: {
      gatewayUrl: "",
      token: "",
      locale: "en",
      sessionKey: "main",
      lastActiveSessionKey: "main",
      theme: "iron",
      themeMode: "dark",
      splitRatio: 0.6,
      navCollapsed: false,
      navGroupsCollapsed: {},
      borderRadius: 50,
      chatFocusMode: false,
      chatShowThinking: false,
    },
    chatMessage: "",
    chatStream: null,
    chatStreamStartedAt: null,
    chatRunId: null,
    chatQueue: [],
    chatMessages: [],
    chatLoading: false,
    chatThinkingLevel: null,
    lastError: null,
    chatAvatarUrl: null,
    basePath: "",
    hello: null,
    agentsList: null,
    agentsPanel: "overview",
    agentsSelectedId: null,
    toolsEffectiveLoading: false,
    toolsEffectiveLoadingKey: null,
    toolsEffectiveResultKey: null,
    toolsEffectiveError: null,
    toolsEffectiveResult: null,
    applySettings(next: AppViewState["settings"]) {
      state.settings = next;
    },
    loadAssistantIdentity: vi.fn(),
    resetToolStream: vi.fn(),
    resetChatScroll: vi.fn(),
  } as unknown as AppViewState & {
    client: GatewayBrowserClient;
    settings: AppViewState["settings"];
  };
  return { state, request };
}

function flushTasks() {
  return new Promise<void>((resolve) => setTimeout(resolve, 0));
}

function createProps(overrides: Partial<ChatProps> = {}): ChatProps {
  return {
    sessionKey: "main",
    onSessionKeyChange: () => undefined,
    thinkingLevel: null,
    showThinking: false,
    showToolCalls: true,
    loading: false,
    sending: false,
    canAbort: false,
    compactionStatus: null,
    fallbackStatus: null,
    messages: [],
    toolMessages: [],
    streamSegments: [],
    stream: null,
    streamStartedAt: null,
    assistantAvatarUrl: null,
    draft: "",
    queue: [],
    connected: true,
    canSend: true,
    disabledReason: null,
    error: null,
    sessions: createSessions(),
    focusMode: false,
    assistantName: "IronAgent",
    assistantAvatar: null,
    onRefresh: () => undefined,
    onToggleFocusMode: () => undefined,
    onDraftChange: () => undefined,
    onSend: () => undefined,
    onQueueRemove: () => undefined,
    onNewSession: () => undefined,
    agentsList: null,
    currentAgentId: "",
    onAgentChange: () => undefined,
    ...overrides,
  };
}

function createOverviewProps(overrides: Partial<OverviewProps> = {}): OverviewProps {
  return {
    connected: false,
    hello: null,
    settings: {
      gatewayUrl: "",
      token: "",
      sessionKey: "main",
      lastActiveSessionKey: "main",
      theme: "iron",
      themeMode: "system",
      chatFocusMode: false,
      chatShowThinking: true,
      chatShowToolCalls: true,
      splitRatio: 0.6,
      navCollapsed: false,
      navWidth: 220,
      navGroupsCollapsed: {},
      borderRadius: 50,
      locale: "en",
    },
    password: "",
    lastError: null,
    lastErrorCode: null,
    presenceCount: 0,
    sessionsCount: null,
    cronEnabled: null,
    cronNext: null,
    lastChannelsRefresh: null,
    usageResult: null,
    sessionsResult: null,
    skillsReport: null,
    cronJobs: [],
    cronStatus: null,
    attentionItems: [],
    eventLog: [],
    overviewLogLines: [],
    showGatewayToken: false,
    showGatewayPassword: false,
    onSettingsChange: () => undefined,
    onPasswordChange: () => undefined,
    onSessionKeyChange: () => undefined,
    onToggleGatewayTokenVisibility: () => undefined,
    onToggleGatewayPasswordVisibility: () => undefined,
    onConnect: () => undefined,
    onRefresh: () => undefined,
    onNavigate: () => undefined,
    onRefreshLogs: () => undefined,
    ...overrides,
  };
}

function createSkill(overrides: Partial<SkillStatusEntry> & Pick<SkillStatusEntry, "skillKey">) {
  const { skillKey, ...rest } = overrides;
  return {
    name: skillKey,
    description: "Skill description",
    source: "workspace",
    filePath: `/workspace/main/skills/${skillKey}/SKILL.md`,
    baseDir: `/workspace/main/skills/${skillKey}`,
    skillKey,
    always: false,
    disabled: false,
    blockedByAllowlist: false,
    eligible: true,
    requirements: { bins: [], env: [], config: [], os: [] },
    missing: { bins: [], env: [], config: [], os: [] },
    configChecks: [],
    install: [],
    ...rest,
  } satisfies SkillStatusEntry;
}

describe("chat view", () => {
  beforeEach(() => {
    resetChatViewState();
  });

  it("formats display-only aliases for hidden runtime paths", () => {
    expect(formatDisplayPath("/home/node/.ironagent/workspace/main/skills")).toBe(
      "IronAgent 工作空间/main/skills",
    );
    expect(formatDisplayPath("/Users/demo/.ironagent/agents/main/agent/ironagent.json")).toBe(
      "/Users/demo/IronAgent/agents/main/agent/IronAgent 运行配置",
    );
  });

  it("hides the context notice when only cumulative inputTokens exceed the limit", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          sessions: {
            ts: 0,
            path: "",
            count: 1,
            defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: 200_000 },
            sessions: [
              {
                key: "main",
                kind: "direct",
                updatedAt: null,
                inputTokens: 757_300,
                totalTokens: 46_000,
                contextTokens: 200_000,
              },
            ],
          },
        }),
      ),
      container,
    );

    expect(container.textContent).not.toContain("context used");
    expect(container.textContent).not.toContain("757.3k / 200k");
  });

  it("uses totalTokens for the context notice detail when current usage is high", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          sessions: {
            ts: 0,
            path: "",
            count: 1,
            defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: 200_000 },
            sessions: [
              {
                key: "main",
                kind: "direct",
                updatedAt: null,
                inputTokens: 757_300,
                totalTokens: 190_000,
                contextTokens: 200_000,
              },
            ],
          },
        }),
      ),
      container,
    );

    expect(container.textContent).toContain("95% context used");
    expect(container.textContent).toContain("190k / 200k");
    expect(container.textContent).not.toContain("757.3k / 200k");
  });

  it("hides the context notice when totalTokens is missing even if inputTokens is high", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          sessions: {
            ts: 0,
            path: "",
            count: 1,
            defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: 200_000 },
            sessions: [
              {
                key: "main",
                kind: "direct",
                updatedAt: null,
                inputTokens: 500_000,
                contextTokens: 200_000,
              },
            ],
          },
        }),
      ),
      container,
    );

    expect(container.textContent).not.toContain("context used");
  });

  it("hides the context notice when totalTokens is marked stale", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          sessions: {
            ts: 0,
            path: "",
            count: 1,
            defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: 200_000 },
            sessions: [
              {
                key: "main",
                kind: "direct",
                updatedAt: null,
                totalTokens: 190_000,
                totalTokensFresh: false,
                contextTokens: 200_000,
              },
            ],
          },
        }),
      ),
      container,
    );

    expect(container.textContent).not.toContain("context used");
    expect(container.textContent).not.toContain("190k / 200k");
  });

  it("uses the assistant avatar URL for the welcome state when the identity avatar is only initials", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          assistantName: "Assistant",
          assistantAvatar: "A",
          assistantAvatarUrl: "/avatar/main",
        }),
      ),
      container,
    );

    const welcomeImage = container.querySelector<HTMLImageElement>(".agent-chat__welcome > img");
    expect(welcomeImage).not.toBeNull();
    expect(welcomeImage?.getAttribute("src")).toBe("/avatar/main");
  });

  it("renders the chat workbench with workspace, approvals, skills, and tools sections", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workspaceFiles: {
            agentId: "main",
            workspace: "/home/node/.ironagent/workspace/main",
            files: [
              {
                name: "report.docx",
                path: "persistent/report.docx",
                missing: false,
              },
              { name: "draft.pdf", path: "temp/draft.pdf", missing: false },
            ],
          },
          approvals: [
            {
              id: "approval-1",
              kind: "exec",
              request: {
                command: "curl https://example.com",
                resolvedPath: "persistent/report.docx",
              },
              createdAtMs: 1,
              expiresAtMs: 10_000,
            },
          ],
          canResolveGatewayApprovals: true,
          skillsReport: {
            workspaceDir: "/home/node/.ironagent/workspace/main",
            managedSkillsDir: "/workspace/main/skills",
            skills: [
              {
                name: "local-pdf",
                description: "Offline PDF utilities",
                source: "workspace",
                filePath: "/workspace/main/skills/local-pdf/SKILL.md",
                baseDir: "/workspace/main/skills/local-pdf",
                skillKey: "local-pdf",
                always: false,
                disabled: false,
                blockedByAllowlist: false,
                eligible: true,
                requirements: { bins: [], env: [], config: [], os: [] },
                missing: { bins: [], env: [], config: [], os: [] },
                configChecks: [],
                install: [],
              },
            ],
          },
          toolsEffectiveResult: {
            agentId: "main",
            profile: "dangerous",
            groups: [
              {
                id: "core",
                label: "Filesystem",
                source: "core",
                tools: [
                  {
                    id: "read",
                    label: "Read",
                    description: "Read files",
                    rawDescription: "Read files",
                    source: "core",
                  },
                ],
              },
            ],
          },
        }),
      ),
      container,
    );

    expect(container.querySelector(".chat-workbench")).not.toBeNull();
    expect(container.textContent).toContain("Workspace");
    expect(container.textContent).toContain("Approvals");
    expect(container.textContent).toContain("Skills");
    expect(container.textContent).toContain("Live tools");
    expect(container.textContent).toContain("IronAgent 工作空间/main");
    expect(container.textContent).toContain("report.docx");
    expect(container.textContent).toContain("curl https://example.com");
    expect(container.textContent).toContain("local-pdf");
    expect(container.textContent).toContain("Filesystem");
  });

  it("renders actionable IronAgent approvals in the workbench rail", () => {
    const container = document.createElement("div");
    const onApprovalDecision = vi.fn();

    render(
      renderChat(
        createProps({
          approvals: [
            {
              id: "plugin-approval-1",
              kind: "plugin",
              request: { command: "exec", sessionKey: "main" },
              pluginTitle: "需要管理员审批: exec",
              pluginDescription: "tool requires approval",
              pluginMetadata: {
                source: "ironagent",
                approvalId: "approval-1",
                approvalScope: "admin",
                capabilityProfile: "dangerous",
                reason: "tool requires approval",
                target: "persistent/ops.log",
              },
              createdAtMs: 1,
              expiresAtMs: 10_000,
            },
          ],
          canResolveGatewayApprovals: false,
          onApprovalDecision,
        }),
      ),
      container,
    );

    expect(container.textContent).toContain("需要管理员审批: exec");
    expect(container.textContent).toContain("tool requires approval");
    expect(container.textContent).toContain("persistent/ops.log");
    expect(
      Array.from(container.querySelectorAll("button")).some((button) =>
        button.textContent?.includes("Approve once"),
      ),
    ).toBe(true);
    const approveButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Approve once"),
    );
    approveButton?.dispatchEvent(new MouseEvent("click"));
    expect(onApprovalDecision).toHaveBeenCalledWith("plugin-approval-1", "allow-once");
  });

  it("disables chat composition while approval lock is active", () => {
    const container = document.createElement("div");

    render(
      renderChat(
        createProps({
          disabledReason: "当前有待审批操作，聊天已挂起。",
          draft: "please continue",
        }),
      ),
      container,
    );

    const textarea = container.querySelector("textarea");
    const sendButton = container.querySelector(".chat-send-btn");

    expect(container.textContent).toContain("当前有待审批操作，聊天已挂起。");
    expect(textarea?.hasAttribute("disabled")).toBe(true);
    expect(sendButton?.hasAttribute("disabled")).toBe(true);
  });

  it("renders IronAgent approvals even when gateway approval scope is unavailable", () => {
    const container = document.createElement("div");

    render(
      renderExecApprovalPrompt({
        hello: { auth: { scopes: [] } },
        execApprovalQueue: [
          {
            id: "ironagent-admin-1",
            kind: "plugin",
            request: { command: "exec", agentId: "001", sessionKey: "agent:001:main" },
            pluginTitle: "Approval required: exec",
            pluginDescription: "tool requires approval",
            pluginId: "ironagent-hook-bridge",
            pluginMetadata: {
              source: "ironagent",
              approvalId: "approval-1",
              approvalScope: "admin",
            },
            createdAtMs: Date.now(),
            expiresAtMs: Date.now() + 60_000,
          },
        ],
        execApprovalBusy: false,
        execApprovalError: null,
        handleExecApprovalDecision: () => undefined,
      } as never),
      container,
    );

    expect(container.querySelector(".exec-approval-overlay")).not.toBeNull();
    expect(container.textContent).toContain("tool requires approval");
  });

  it("hides gateway-only exec approvals when gateway approval scope is unavailable", () => {
    const container = document.createElement("div");

    render(
      renderExecApprovalPrompt({
        hello: { auth: { scopes: [] } },
        execApprovalQueue: [
          {
            id: "ironagent-admin-1",
            kind: "plugin",
            request: { command: "exec", agentId: "001", sessionKey: "agent:001:main" },
            pluginTitle: "Approval required: exec",
            pluginDescription: "tool requires approval",
            pluginId: "ironagent-hook-bridge",
            pluginMetadata: {
              source: "ironagent",
              approvalId: "approval-1",
              approvalScope: "admin",
            },
            createdAtMs: Date.now(),
            expiresAtMs: Date.now() + 60_000,
          },
          {
            id: "local-exec-1",
            kind: "exec",
            request: { command: "rm -rf /tmp/demo", sessionKey: "main" },
            createdAtMs: Date.now(),
            expiresAtMs: Date.now() + 60_000,
          },
        ],
        execApprovalBusy: false,
        execApprovalError: null,
        handleExecApprovalDecision: () => undefined,
      } as never),
      container,
    );

    expect(container.querySelector(".exec-approval-overlay")).not.toBeNull();
    expect(container.textContent).toContain("tool requires approval");
    expect(container.textContent).not.toContain("rm -rf /tmp/demo");
  });

  it("filters approvals to the active chat assistant or session", () => {
    const approvals = [
      {
        id: "keep-agent",
        kind: "plugin",
        request: {
          command: "write",
          agentId: "assistant-a",
          sessionKey: "agent:assistant-a:main",
        },
        createdAtMs: 1,
        expiresAtMs: 2,
      },
      {
        id: "drop-agent",
        kind: "plugin",
        request: {
          command: "write",
          agentId: "assistant-b",
          sessionKey: "agent:assistant-b:main",
        },
        createdAtMs: 1,
        expiresAtMs: 2,
      },
      {
        id: "keep-session",
        kind: "exec",
        request: { command: "rm demo", sessionKey: "agent:assistant-a:main" },
        createdAtMs: 1,
        expiresAtMs: 2,
      },
      {
        id: "keep-global",
        kind: "exec",
        request: { command: "ls" },
        createdAtMs: 1,
        expiresAtMs: 2,
      },
    ] as const;

    expect(
      filterExecApprovalsForChat(approvals, {
        activeChatAgentId: "assistant-a",
        sessionKey: "agent:assistant-a:main",
      }).map((entry) => entry.id),
    ).toEqual(["keep-agent", "keep-session", "keep-global"]);
  });

  it("renders aliased skill detail paths through the shared display formatter", async () => {
    const container = document.createElement("div");
    const originalShowModalDescriptor = Object.getOwnPropertyDescriptor(
      HTMLDialogElement.prototype,
      "showModal",
    );
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      writable: true,
      value(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      },
    });

    try {
      render(
        renderSkills({
          connected: true,
          loading: false,
          report: {
            workspaceDir: "/home/node/.ironagent/workspace/main",
            managedSkillsDir: "/home/node/.ironagent/workspace/main/skills",
            skills: [
              createSkill({
                skillKey: "repo-skill",
                filePath: "/Users/demo/.ironagent/skills/repo-skill/ironagent.json",
              }),
            ],
          },
          error: null,
          filter: "",
          statusFilter: "enabled",
          edits: {},
          busyKey: null,
          messages: {},
          detailKey: "repo-skill",
          onFilterChange: () => undefined,
          onStatusFilterChange: () => undefined,
          onRefresh: () => undefined,
          onToggle: () => undefined,
          onEdit: () => undefined,
          onSaveKey: () => undefined,
          onInstall: () => undefined,
          onDetailOpen: () => undefined,
          onDetailClose: () => undefined,
        }),
        container,
      );
      await Promise.resolve();

      expect(container.textContent).toContain(
        "/Users/demo/IronAgent/skills/repo-skill/IronAgent 运行配置",
      );
    } finally {
      if (originalShowModalDescriptor) {
        Object.defineProperty(
          HTMLDialogElement.prototype,
          "showModal",
          originalShowModalDescriptor,
        );
      } else {
        Reflect.deleteProperty(
          HTMLDialogElement.prototype as HTMLDialogElement & Record<string, unknown>,
          "showModal",
        );
      }
    }
  });

  it("renders aliased workspace paths in the overview and core files panels", async () => {
    const container = document.createElement("div");
    render(
      html`
        ${renderAgentOverview({
          agent: {
            id: "main",
            name: "Main",
            model: "openai/gpt-5.1",
            workspace: "/home/node/.ironagent/workspace/main",
          } as never,
          basePath: "",
          defaultId: "main",
          configForm: null,
          agentFilesList: {
            agentId: "main",
            workspace: "/home/node/.ironagent/workspace/main",
            files: [],
          },
          agentIdentity: null,
          agentIdentityLoading: false,
          agentIdentityError: null,
          configLoading: false,
          configSaving: false,
          configDirty: false,
          modelCatalog: [],
          onConfigReload: () => undefined,
          onConfigSave: () => undefined,
          onModelChange: () => undefined,
          onModelFallbacksChange: () => undefined,
          onSelectPanel: () => undefined,
        })}
        ${renderAgentFiles({
          agentId: "main",
          agentFilesList: {
            agentId: "main",
            workspace: "/home/node/.ironagent/workspace/main",
            files: [
              {
                name: "IDENTITY.md",
                path: "/home/node/.ironagent/workspace/main/IDENTITY.md",
                missing: false,
              },
            ],
          },
          agentFilesLoading: false,
          agentFilesError: null,
          agentFileActive: "IDENTITY.md",
          agentFileContents: { "IDENTITY.md": "# hello" },
          agentFileDrafts: {},
          agentFileSaving: false,
          onLoadFiles: () => undefined,
          onSelectFile: () => undefined,
          onFileDraftChange: () => undefined,
          onFileReset: () => undefined,
          onFileSave: () => undefined,
        })}
      `,
      container,
    );
    await Promise.resolve();

    expect(container.textContent).toContain("IronAgent 工作空间/main");
    expect(container.textContent).toContain("IronAgent 工作空间/main/IDENTITY.md");
  });

  it("uses the inline workspace file url for the preview action", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workspaceFiles: {
            agentId: "main",
            workspace: "/workspace/main",
            files: [
              {
                name: "report.pdf",
                path: "persistent/report.pdf",
                missing: false,
                viewUrl: "/api/v1/workspace-files/file-1/download?disposition=inline",
                downloadUrl: "/api/v1/workspace-files/file-1/download?disposition=attachment",
              },
            ],
          },
        }),
      ),
      container,
    );

    const actions = container.querySelectorAll<HTMLAnchorElement>(".chat-workbench__item-action");
    expect(actions).toHaveLength(2);
    expect(actions[0]?.getAttribute("href")).toBe(
      "/api/v1/workspace-files/file-1/download?disposition=inline",
    );
    expect(actions[1]?.getAttribute("href")).toBe(
      "/api/v1/workspace-files/file-1/download?disposition=attachment",
    );
  });

  it("calls the workspace file delete handler after confirmation", () => {
    const container = document.createElement("div");
    const onDeleteWorkspaceFile = vi.fn();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      render(
        renderChat(
          createProps({
            workspaceFiles: {
              agentId: "main",
              workspace: "/workspace/main",
              files: [
                {
                  name: "report.pdf",
                  path: "persistent/report.pdf",
                  missing: false,
                  fileId: "file-1",
                  viewUrl: "/api/v1/workspace-files/file-1/download?disposition=inline",
                  downloadUrl: "/api/v1/workspace-files/file-1/download?disposition=attachment",
                },
              ],
            },
            onDeleteWorkspaceFile,
          }),
        ),
        container,
      );

      container.querySelector<HTMLButtonElement>('[aria-label="Delete workspace file"]')?.click();

      expect(confirmSpy).toHaveBeenCalledWith('Delete workspace file "report.pdf"?');
      expect(onDeleteWorkspaceFile).toHaveBeenCalledWith("file-1", "persistent");
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it("shows only persistent and temp workspace buckets", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workspaceFiles: {
            agentId: "demo-main",
            workspace: "/workspace/demo-main",
            files: [
              {
                name: "report.pdf",
                path: "persistent/report.pdf",
                missing: false,
                bucket: "persistent",
              },
              {
                name: "draft.txt",
                path: "temp/draft.txt",
                missing: false,
                bucket: "temp",
              },
            ],
          },
          agentFiles: {
            agentId: "main",
            workspace: "/home/node/.ironagent/workspace/main",
            files: [
              {
                name: "USER.md",
                path: "/home/node/.ironagent/workspace/main/USER.md",
                missing: false,
              },
              {
                name: "SOUL.md",
                path: "/home/node/.ironagent/workspace/main/SOUL.md",
                missing: false,
              },
            ],
          },
        }),
      ),
      container,
    );

    expect(container.textContent).toContain("persistent");
    expect(container.textContent).toContain("temp");
    expect(container.textContent).toContain("report.pdf");
    expect(container.textContent).toContain("draft.txt");
    expect(container.textContent).not.toContain("assistant files");
    expect(container.textContent).not.toContain("USER.md");
    expect(container.textContent).not.toContain("SOUL.md");
  });

  it("does not mix agent files into workspace buckets", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workspaceFiles: {
            agentId: "demo-main",
            workspace: "/workspace/demo-main",
            files: [],
          },
          agentFiles: {
            agentId: "main",
            workspace: "/home/node/.ironagent/workspace/main",
            files: [
              {
                name: "report.docx",
                path: "/home/node/.ironagent/workspace/main/persistent/report.docx",
                missing: false,
              },
              {
                name: "draft.txt",
                path: "/home/node/.ironagent/workspace/main/temp/draft.txt",
                missing: false,
              },
              {
                name: "USER.md",
                path: "/home/node/.ironagent/workspace/main/USER.md",
                missing: false,
              },
            ],
          },
        }),
      ),
      container,
    );

    const groups = [...container.querySelectorAll(".chat-workbench__group")].map(
      (group) => group.textContent ?? "",
    );
    expect(groups).toHaveLength(2);
    expect(groups[0]).toContain("persistent");
    expect(groups[0]).not.toContain("report.docx");
    expect(groups[1]).toContain("temp");
    expect(groups[1]).not.toContain("draft.txt");
    expect(container.textContent).not.toContain("USER.md");
  });

  it("renders nested workspace folders inside collapsible bucket groups", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workspaceFiles: {
            agentId: "demo-main",
            workspace: "/workspace/demo-main",
            files: [
              {
                name: "q1-report.xlsx",
                path: "persistent/reports/2026/q1-report.xlsx",
                missing: false,
                bucket: "persistent",
              },
              {
                name: "scratch.txt",
                path: "temp/drafts/scratch.txt",
                missing: false,
                bucket: "temp",
              },
            ],
          },
          agentFiles: {
            agentId: "main",
            workspace: "/home/node/.ironagent/workspace/main",
            files: [
              {
                name: "USER.md",
                path: "/home/node/.ironagent/workspace/main/USER.md",
                missing: false,
              },
            ],
          },
        }),
      ),
      container,
    );

    const bucketGroups = container.querySelectorAll<HTMLDetailsElement>(
      ".chat-workbench__group--collapsible",
    );
    expect(bucketGroups).toHaveLength(2);
    expect(bucketGroups[0]?.open).toBe(true);
    expect(bucketGroups[0]?.textContent).toContain("persistent/");
    expect(bucketGroups[0]?.textContent).toContain("reports/");
    expect(bucketGroups[0]?.textContent).toContain("2026/");
    expect(bucketGroups[0]?.textContent).toContain("q1-report.xlsx");
    expect(bucketGroups[1]?.textContent).toContain("temp/");
    expect(bucketGroups[1]?.textContent).toContain("drafts/");
    expect(bucketGroups[1]?.textContent).toContain("scratch.txt");
  });

  it("stages selected files without uploading before send", async () => {
    const container = document.createElement("div");
    let attachments: ChatProps["attachments"] = [];
    const onUploadWorkspaceFiles = vi.fn().mockResolvedValue([
      {
        status: "pending_approval",
        assistantId: "main",
        filename: "report.pdf",
        bucket: "persistent",
        path: "persistent/report.pdf",
      },
    ]);
    const props = createProps({
      attachments,
      onUploadWorkspaceFiles,
      onAttachmentsChange: (next) => {
        attachments = next;
        props.attachments = next;
        render(renderChat(props), container);
      },
    });
    props.onRequestUpdate = () => {
      render(renderChat(props), container);
    };

    render(renderChat(props), container);

    const input = container.querySelector<HTMLInputElement>(".agent-chat__file-input");
    expect(input).not.toBeNull();
    Object.defineProperty(input as HTMLInputElement, "files", {
      configurable: true,
      value: [new File(["pdf"], "report.pdf", { type: "application/pdf" })],
    });
    input?.dispatchEvent(new Event("change"));
    await flushTasks();

    expect(onUploadWorkspaceFiles).not.toHaveBeenCalled();
    expect(attachments).toHaveLength(1);
    expect(container.textContent).toContain("report.pdf");
    expect(container.textContent).not.toContain("不会自动进入工作区");
    expect(container.textContent).not.toContain("waiting for approval");
  });

  it("uploads workspace files only from the explicit workbench upload action", async () => {
    const container = document.createElement("div");
    const onUploadWorkspaceFiles = vi.fn().mockResolvedValue([
      {
        status: "accepted",
        assistantId: "main",
        filename: "report.pdf",
        bucket: "persistent",
        path: "persistent/report.pdf",
      },
    ]);
    const props = createProps({
      workspaceFiles: {
        agentId: "main",
        workspace: "/workspace/main",
        files: [],
      },
      onUploadWorkspaceFiles,
    });
    props.onRequestUpdate = () => {
      render(renderChat(props), container);
    };

    render(renderChat(props), container);

    const input = container.querySelector<HTMLInputElement>(".chat-workbench__upload-input");
    expect(input).not.toBeNull();
    Object.defineProperty(input as HTMLInputElement, "files", {
      configurable: true,
      value: [new File(["pdf"], "report.pdf", { type: "application/pdf" })],
    });
    input?.dispatchEvent(new Event("change"));
    await flushTasks();

    expect(onUploadWorkspaceFiles).toHaveBeenCalledWith([expect.any(File)]);
    expect(container.textContent).toContain("Uploaded 1 file(s) to the workspace.");
  });

  it("keeps dropped files staged in the composer until send", async () => {
    const container = document.createElement("div");
    let attachments: ChatProps["attachments"] = [];
    const props = createProps({
      attachments,
      onAttachmentsChange: (next) => {
        attachments = next;
        props.attachments = next;
        render(renderChat(props), container);
      },
    });
    props.onRequestUpdate = () => {
      render(renderChat(props), container);
    };

    render(renderChat(props), container);

    const input = container.querySelector<HTMLInputElement>(".agent-chat__file-input");
    expect(input).not.toBeNull();
    Object.defineProperty(input as HTMLInputElement, "files", {
      configurable: true,
      value: [new File(["pdf"], "report.pdf", { type: "application/pdf" })],
    });
    input?.dispatchEvent(new Event("change"));
    await flushTasks();

    expect(attachments).toHaveLength(1);
    expect(attachments?.[0]).toMatchObject({
      name: "report.pdf",
      mimeType: "application/pdf",
    });
    expect(container.textContent).toContain("report.pdf");
  });

  it("renders collapsed workbench metrics when the workbench is collapsed", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workbenchCollapsed: true,
          workspaceFiles: {
            agentId: "main",
            workspace: "/workspace/main",
            files: [{ name: "report.docx", path: "persistent/report.docx", missing: false }],
          },
          approvals: [
            {
              id: "approval-1",
              kind: "exec",
              request: { command: "curl https://example.com" },
              createdAtMs: 1,
              expiresAtMs: 10_000,
            },
          ],
          skillsReport: {
            workspaceDir: "/workspace/main",
            managedSkillsDir: "/workspace/main/skills",
            skills: [],
          },
          toolsEffectiveResult: {
            agentId: "main",
            profile: "dangerous",
            groups: [],
          },
        }),
      ),
      container,
    );

    expect(container.querySelector(".chat-workbench--collapsed")).not.toBeNull();
    expect(container.querySelector(".chat-workbench__body")).toBeNull();
    expect(container.querySelector(".chat-workbench__logo")).not.toBeNull();
    expect(container.textContent).not.toContain("Workbench");
    expect(container.querySelectorAll(".chat-workbench__collapsed-metric")).toHaveLength(4);
  });

  it("filters the workbench to configured skills and hides setup-only entries", () => {
    const container = document.createElement("div");
    const props = createProps({
      skillsReport: {
        workspaceDir: "/workspace/main",
        managedSkillsDir: "/workspace/main/skills",
        skills: [
          createSkill({ skillKey: "bear-notes", name: "bear-notes" }),
          createSkill({
            skillKey: "apple-notes",
            name: "apple-notes",
            eligible: false,
            missing: { bins: ["memo"], env: [], config: [], os: [] },
          }),
          createSkill({
            skillKey: "1password",
            name: "1password",
            disabled: true,
          }),
        ],
      },
    });
    props.onRequestUpdate = () => {
      render(renderChat(props), container);
    };

    render(renderChat(props), container);

    const readyChip = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".chat-workbench__stat--button"),
    ).find((button) => button.textContent?.includes("in use"));
    expect(readyChip).not.toBeUndefined();
    expect(container.textContent).not.toContain("apple-notes");

    const filteredTitles = Array.from(
      container.querySelectorAll(".chat-workbench__item--skill .chat-workbench__item-title"),
    ).map((node) => node.textContent?.trim());
    expect(filteredTitles).toEqual(["bear-notes"]);
    expect(readyChip?.classList.contains("is-active")).toBe(true);

    const disabledChip = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".chat-workbench__stat--button"),
    ).find((button) => button.textContent?.toLowerCase().includes("disabled"));
    expect(disabledChip).not.toBeUndefined();
    disabledChip?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    const disabledTitles = Array.from(
      container.querySelectorAll(".chat-workbench__item--skill .chat-workbench__item-title"),
    ).map((node) => node.textContent?.trim());
    expect(disabledTitles).toEqual(["1password"]);
  });

  it("shows only current workspace skills in the workbench skill list and counts", () => {
    const container = document.createElement("div");
    const props = createProps({
      skillsReport: {
        workspaceDir: "/workspace/H_demo",
        managedSkillsDir: "/home/node/.ironagent/skills",
        skills: [
          createSkill({
            skillKey: "healthcheck",
            name: "healthcheck",
            source: "ironagent-bundled",
          }),
          createSkill({
            skillKey: "weather",
            name: "weather",
            source: "ironagent-bundled",
          }),
          createSkill({
            skillKey: "ironflow",
            name: "ironflow",
            source: "ironagent-managed",
            disabled: true,
          }),
          createSkill({
            skillKey: "table-process",
            name: "table-process",
            source: "ironagent-workspace",
            filePath: "/workspace/H_demo/skills/table-process/SKILL.md",
            baseDir: "/workspace/H_demo/skills/table-process",
          }),
        ],
      },
    });
    props.onRequestUpdate = () => {
      render(renderChat(props), container);
    };

    render(renderChat(props), container);

    const readyChip = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".chat-workbench__stat--button"),
    ).find((button) => button.textContent?.includes("in use"));
    const disabledChip = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".chat-workbench__stat--button"),
    ).find((button) => button.textContent?.toLowerCase().includes("disabled"));
    expect(readyChip?.textContent).toContain("1");
    expect(disabledChip?.textContent).toContain("0");

    const skillTitles = Array.from(
      container.querySelectorAll(".chat-workbench__item--skill .chat-workbench__item-title"),
    ).map((node) => node.textContent?.trim());
    expect(skillTitles).toEqual(["table-process"]);
    expect(container.textContent).not.toContain("healthcheck");
    expect(container.textContent).not.toContain("weather");
    expect(container.textContent).not.toContain("ironflow");
  });

  it("restores skill toggles in the workbench list", () => {
    const container = document.createElement("div");
    const onToggleSkillEnabled = vi.fn();
    const props = createProps({
      skillsReport: {
        workspaceDir: "/workspace/main",
        managedSkillsDir: "/workspace/main/skills",
        skills: [
          createSkill({
            skillKey: "1password",
            name: "1password",
            disabled: true,
          }),
        ],
      },
      onToggleSkillEnabled,
    });

    props.onRequestUpdate = () => {
      render(renderChat(props), container);
    };

    render(renderChat(props), container);

    const [, disabledChip] = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".chat-workbench__stat--button"),
    );
    expect(disabledChip).not.toBeUndefined();
    disabledChip?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    const toggle = container.querySelector<HTMLButtonElement>(
      '.chat-workbench__skill-toggle[data-skill-key="1password"]',
    );
    expect(toggle).not.toBeNull();
    expect(toggle?.textContent).toContain("Disabled");

    toggle?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(onToggleSkillEnabled).toHaveBeenCalledWith("1password", true);
  });

  it("marks compact dual-rail layout when the workbench is collapsed and the detail sidebar is open", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          workbenchCollapsed: true,
          sidebarOpen: true,
          sidebarContent: "# Details",
          onCloseSidebar: () => undefined,
          onOpenSidebar: () => undefined,
        }),
      ),
      container,
    );

    const stage = container.querySelector(".chat-stage");
    expect(stage?.classList.contains("chat-stage--sidebar-open")).toBe(true);
    expect(stage?.classList.contains("chat-stage--workbench-collapsed")).toBe(true);
    expect(stage?.classList.contains("chat-stage--dual-rail-compact")).toBe(true);
    expect(container.querySelector(".chat-sidebar")).not.toBeNull();
  });

  it("falls back to the bundled logo in the welcome state when the assistant avatar is not a URL", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          assistantName: "Assistant",
          assistantAvatar: "A",
          assistantAvatarUrl: null,
        }),
      ),
      container,
    );

    const welcomeImage = container.querySelector<HTMLImageElement>(".agent-chat__welcome > img");
    const logoImage = container.querySelector<HTMLImageElement>(
      ".agent-chat__welcome .agent-chat__avatar--logo img",
    );
    expect(welcomeImage).toBeNull();
    expect(logoImage).not.toBeNull();
    expect(logoImage?.getAttribute("src")).toBe("ironagent-logo.png");
  });

  it("keeps the welcome logo fallback under the mounted base path", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          assistantName: "Assistant",
          assistantAvatar: "A",
          assistantAvatarUrl: null,
          basePath: "/ironagent/",
        }),
      ),
      container,
    );

    const logoImage = container.querySelector<HTMLImageElement>(
      ".agent-chat__welcome .agent-chat__avatar--logo img",
    );
    expect(logoImage).not.toBeNull();
    expect(logoImage?.getAttribute("src")).toBe("/ironagent/ironagent-logo.png");
  });

  it("keeps grouped assistant avatar fallbacks under the mounted base path", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          assistantName: "Assistant",
          assistantAvatar: "A",
          assistantAvatarUrl: null,
          basePath: "/ironagent/",
          messages: [
            {
              role: "assistant",
              content: "hello",
              timestamp: 1000,
            },
          ],
        }),
      ),
      container,
    );

    const groupedLogo = container.querySelector<HTMLImageElement>(
      ".chat-group.assistant .chat-avatar--logo",
    );
    expect(groupedLogo).not.toBeNull();
    expect(groupedLogo?.getAttribute("src")).toBe("/ironagent/ironagent-logo.png");
  });

  it("keeps the persisted overview locale selected before i18n hydration finishes", async () => {
    const container = document.createElement("div");
    const props = createOverviewProps({
      settings: {
        ...createOverviewProps().settings,
        locale: "zh-CN",
      },
    });

    getSafeLocalStorage()?.clear();
    await i18n.setLocale("en");

    render(renderOverview(props), container);
    await Promise.resolve();

    let select = container.querySelector<HTMLSelectElement>("select");
    expect(i18n.getLocale()).toBe("en");
    expect(select?.value).toBe("zh-CN");
    expect(select?.selectedOptions[0]?.textContent?.trim()).toBe("简体中文 (Simplified Chinese)");

    await i18n.setLocale("zh-CN");
    render(renderOverview(props), container);
    await Promise.resolve();

    select = container.querySelector<HTMLSelectElement>("select");
    expect(select?.value).toBe("zh-CN");
    expect(select?.selectedOptions[0]?.textContent?.trim()).toBe("简体中文 (简体中文)");

    await i18n.setLocale("en");
  });

  it("renders compacting indicator as a badge", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          compactionStatus: {
            phase: "active",
            runId: "run-1",
            startedAt: Date.now(),
            completedAt: null,
          },
        }),
      ),
      container,
    );

    const indicator = container.querySelector(".compaction-indicator--active");
    expect(indicator).not.toBeNull();
    expect(indicator?.textContent).toContain("Compacting context...");
  });

  it("renders retry-pending compaction indicator as a badge", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          compactionStatus: {
            phase: "retrying",
            runId: "run-1",
            startedAt: Date.now(),
            completedAt: null,
          },
        }),
      ),
      container,
    );

    const indicator = container.querySelector(".compaction-indicator--active");
    expect(indicator).not.toBeNull();
    expect(indicator?.textContent).toContain("Retrying after compaction...");
  });

  it("renders completion indicator shortly after compaction", () => {
    const container = document.createElement("div");
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_000);
    render(
      renderChat(
        createProps({
          compactionStatus: {
            phase: "complete",
            runId: "run-1",
            startedAt: 900,
            completedAt: 900,
          },
        }),
      ),
      container,
    );

    const indicator = container.querySelector(".compaction-indicator--complete");
    expect(indicator).not.toBeNull();
    expect(indicator?.textContent).toContain("Context compacted");
    nowSpy.mockRestore();
  });

  it("hides stale compaction completion indicator", () => {
    const container = document.createElement("div");
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(10_000);
    render(
      renderChat(
        createProps({
          compactionStatus: {
            phase: "complete",
            runId: "run-1",
            startedAt: 0,
            completedAt: 0,
          },
        }),
      ),
      container,
    );

    expect(container.querySelector(".compaction-indicator")).toBeNull();
    nowSpy.mockRestore();
  });

  it("renders fallback indicator shortly after fallback event", () => {
    const container = document.createElement("div");
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_000);
    render(
      renderChat(
        createProps({
          fallbackStatus: {
            selected: "fireworks/minimax-m2p5",
            active: "deepinfra/moonshotai/Kimi-K2.5",
            attempts: ["fireworks/minimax-m2p5: rate limit"],
            occurredAt: 900,
          },
        }),
      ),
      container,
    );

    const indicator = container.querySelector(".compaction-indicator--fallback");
    expect(indicator).not.toBeNull();
    expect(indicator?.textContent).toContain("Fallback active: deepinfra/moonshotai/Kimi-K2.5");
    nowSpy.mockRestore();
  });

  it("hides stale fallback indicator", () => {
    const container = document.createElement("div");
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(20_000);
    render(
      renderChat(
        createProps({
          fallbackStatus: {
            selected: "fireworks/minimax-m2p5",
            active: "deepinfra/moonshotai/Kimi-K2.5",
            attempts: [],
            occurredAt: 0,
          },
        }),
      ),
      container,
    );

    expect(container.querySelector(".compaction-indicator--fallback")).toBeNull();
    nowSpy.mockRestore();
  });

  it("renders fallback-cleared indicator shortly after transition", () => {
    const container = document.createElement("div");
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_000);
    render(
      renderChat(
        createProps({
          fallbackStatus: {
            phase: "cleared",
            selected: "fireworks/minimax-m2p5",
            active: "fireworks/minimax-m2p5",
            previous: "deepinfra/moonshotai/Kimi-K2.5",
            attempts: [],
            occurredAt: 900,
          },
        }),
      ),
      container,
    );

    const indicator = container.querySelector(".compaction-indicator--fallback-cleared");
    expect(indicator).not.toBeNull();
    expect(indicator?.textContent).toContain("Fallback cleared: fireworks/minimax-m2p5");
    nowSpy.mockRestore();
  });

  it("shows a stop button when aborting is available", () => {
    const container = document.createElement("div");
    const onAbort = vi.fn();
    const onSend = vi.fn();
    render(
      renderChat(
        createProps({
          canAbort: true,
          sending: true,
          onAbort,
          onSend,
          draft: "follow up",
        }),
      ),
      container,
    );

    const stopButton = container.querySelector<HTMLButtonElement>(
      'button[title="Stop generating"]',
    );
    expect(stopButton).not.toBeUndefined();
    stopButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onAbort).toHaveBeenCalledTimes(1);
    const queueButton = container.querySelector<HTMLButtonElement>('button[title="Queue message"]');
    expect(queueButton).not.toBeNull();
    expect(queueButton?.disabled).toBe(false);
    queueButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(container.textContent).not.toContain("New session");
  });

  it("renders queued messages inside the composer area", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          queue: [
            {
              id: "queued-1",
              text: "next question",
              createdAt: Date.now(),
            },
          ],
        }),
      ),
      container,
    );

    const composerQueue = container.querySelector(".agent-chat__input .chat-queue");
    expect(composerQueue).not.toBeNull();
    expect(composerQueue?.textContent).toContain("Queued (1)");
    expect(composerQueue?.textContent).toContain("next question");
  });

  it("shows a new session button when aborting is unavailable", () => {
    const container = document.createElement("div");
    const onNewSession = vi.fn();
    render(
      renderChat(
        createProps({
          canAbort: false,
          onNewSession,
        }),
      ),
      container,
    );

    const newSessionButton = container.querySelector<HTMLButtonElement>(
      'button[title="New session"]',
    );
    expect(newSessionButton).not.toBeUndefined();
    newSessionButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onNewSession).toHaveBeenCalledTimes(1);
    expect(container.textContent).not.toContain("Stop");
  });

  it("shows sender labels from sanitized gateway messages instead of generic You", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          messages: [
            {
              role: "user",
              content: "hello from topic",
              senderLabel: "Iris",
              timestamp: 1000,
            },
          ],
        }),
      ),
      container,
    );

    const senderLabels = Array.from(container.querySelectorAll(".chat-sender-name")).map((node) =>
      node.textContent?.trim(),
    );
    expect(senderLabels).toContain("Iris");
    expect(senderLabels).not.toContain("You");
  });

  it("keeps consecutive user messages from different senders in separate groups", () => {
    const container = document.createElement("div");
    render(
      renderChat(
        createProps({
          messages: [
            {
              role: "user",
              content: "first",
              senderLabel: "Iris",
              timestamp: 1000,
            },
            {
              role: "user",
              content: "second",
              senderLabel: "Joaquin De Rojas",
              timestamp: 1001,
            },
          ],
        }),
      ),
      container,
    );

    const groups = container.querySelectorAll(".chat-group.user");
    expect(groups).toHaveLength(2);
    const senderLabels = Array.from(container.querySelectorAll(".chat-sender-name")).map((node) =>
      node.textContent?.trim(),
    );
    expect(senderLabels).toContain("Iris");
    expect(senderLabels).toContain("Joaquin De Rojas");
  });

  it("opens delete confirm on the left for user messages", () => {
    const originalPreference = readDeleteConfirmPreference();
    clearDeleteConfirmPreference();
    const container = document.createElement("div");
    try {
      render(
        renderChat(
          createProps({
            messages: [
              {
                role: "user",
                content: "hello from user",
                timestamp: 1000,
              },
            ],
          }),
        ),
        container,
      );

      const deleteButton = container.querySelector<HTMLButtonElement>(
        ".chat-group.user .chat-group-delete",
      );
      expect(deleteButton).not.toBeNull();
      deleteButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

      const confirm = container.querySelector<HTMLElement>(".chat-group.user .chat-delete-confirm");
      expect(confirm).not.toBeNull();
      expect(confirm?.classList.contains("chat-delete-confirm--left")).toBe(true);
    } finally {
      restoreDeleteConfirmPreference(originalPreference);
    }
  });

  it("opens delete confirm on the right for assistant messages", () => {
    const originalPreference = readDeleteConfirmPreference();
    clearDeleteConfirmPreference();
    const container = document.createElement("div");
    try {
      render(
        renderChat(
          createProps({
            messages: [
              {
                role: "assistant",
                content: "hello from assistant",
                timestamp: 1000,
              },
            ],
          }),
        ),
        container,
      );

      const deleteButton = container.querySelector<HTMLButtonElement>(
        ".chat-group.assistant .chat-group-delete",
      );
      expect(deleteButton).not.toBeNull();
      deleteButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

      const confirm = container.querySelector<HTMLElement>(
        ".chat-group.assistant .chat-delete-confirm",
      );
      expect(confirm).not.toBeNull();
      expect(confirm?.classList.contains("chat-delete-confirm--right")).toBe(true);
    } finally {
      restoreDeleteConfirmPreference(originalPreference);
    }
  });

  it("renders delete confirm with the expected safe structure", () => {
    const originalPreference = readDeleteConfirmPreference();
    clearDeleteConfirmPreference();
    const container = document.createElement("div");
    try {
      render(
        renderChat(
          createProps({
            messages: [
              {
                role: "assistant",
                content: "hello from assistant",
                timestamp: 1000,
              },
            ],
          }),
        ),
        container,
      );

      const deleteButton = container.querySelector<HTMLButtonElement>(
        ".chat-group.assistant .chat-group-delete",
      );
      expect(deleteButton).not.toBeNull();
      deleteButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

      const confirm = container.querySelector<HTMLElement>(
        ".chat-group.assistant .chat-delete-confirm",
      );
      expect(confirm?.querySelector(".chat-delete-confirm__text")?.textContent).toBe(
        "Delete this message?",
      );
      expect(confirm?.querySelector(".chat-delete-confirm__remember span")?.textContent).toBe(
        "Don't ask again",
      );
      expect(confirm?.querySelector<HTMLButtonElement>(".chat-delete-confirm__cancel")?.type).toBe(
        "button",
      );
      expect(confirm?.querySelector<HTMLButtonElement>(".chat-delete-confirm__yes")?.type).toBe(
        "button",
      );
      expect(confirm?.querySelector<HTMLInputElement>(".chat-delete-confirm__check")?.type).toBe(
        "checkbox",
      );
    } finally {
      restoreDeleteConfirmPreference(originalPreference);
    }
  });

  it("patches the current session model from the chat header picker", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
      } satisfies Partial<Response>),
    );
    const { state, request } = createChatHeaderState();
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect).not.toBeNull();
    expect(modelSelect?.value).toBe("");

    modelSelect!.value = "openai/gpt-5-mini";
    modelSelect!.dispatchEvent(new Event("change", { bubbles: true }));
    await flushTasks();

    expect(request).toHaveBeenCalledWith("sessions.patch", {
      key: "main",
      model: "openai/gpt-5-mini",
    });
    expect(request).not.toHaveBeenCalledWith("chat.history", expect.anything());
    expect(state.sessionsResult?.sessions[0]?.model).toBe("gpt-5-mini");
    expect(state.sessionsResult?.sessions[0]?.modelProvider).toBe("openai");
    vi.unstubAllGlobals();
  });

  it("reloads effective tools after a chat-header model switch for the active tools panel", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
      } satisfies Partial<Response>),
    );
    const { state, request } = createChatHeaderState();
    state.agentsPanel = "tools";
    state.agentsSelectedId = "main";
    state.toolsEffectiveResultKey = "main:main";
    state.toolsEffectiveResult = {
      agentId: "main",
      profile: "coding",
      groups: [],
    };
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect).not.toBeNull();

    modelSelect!.value = "openai/gpt-5-mini";
    modelSelect!.dispatchEvent(new Event("change", { bubbles: true }));
    await flushTasks();

    expect(request).toHaveBeenCalledWith("tools.effective", {
      agentId: "main",
      sessionKey: "main",
    });
    expect(state.toolsEffectiveResultKey).toBe("main:main:model=openai/gpt-5-mini");
    vi.unstubAllGlobals();
  });

  it("clears the session model override back to the default model", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
      } satisfies Partial<Response>),
    );
    const { state, request } = createChatHeaderState({ model: "gpt-5-mini" });
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect).not.toBeNull();
    expect(modelSelect?.value).toBe("openai/gpt-5-mini");

    modelSelect!.value = "";
    modelSelect!.dispatchEvent(new Event("change", { bubbles: true }));
    await flushTasks();

    expect(request).toHaveBeenCalledWith("sessions.patch", {
      key: "main",
      model: null,
    });
    expect(state.sessionsResult?.sessions[0]?.model).toBeUndefined();
    vi.unstubAllGlobals();
  });

  it("disables the chat header model picker while a run is active", () => {
    const { state } = createChatHeaderState();
    state.chatRunId = "run-123";
    state.chatStream = "Working";
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect).not.toBeNull();
    expect(modelSelect?.disabled).toBe(true);
  });

  it("keeps the selected model visible when the active session is absent from sessions.list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
      } satisfies Partial<Response>),
    );
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect).not.toBeNull();

    modelSelect!.value = "openai/gpt-5-mini";
    modelSelect!.dispatchEvent(new Event("change", { bubbles: true }));
    await flushTasks();
    render(renderChatSessionSelect(state), container);

    const rerendered = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(rerendered?.value).toBe("openai/gpt-5-mini");
    vi.unstubAllGlobals();
  });

  it("normalizes cached bare /model overrides to the matching catalog option", () => {
    const { state } = createChatHeaderState();
    state.chatModelOverrides = { main: { kind: "raw", value: "gpt-5-mini" } };

    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect).not.toBeNull();
    expect(modelSelect?.value).toBe("openai/gpt-5-mini");

    const optionValues = Array.from(modelSelect?.querySelectorAll("option") ?? []).map(
      (option) => option.value,
    );
    expect(optionValues).toContain("openai/gpt-5-mini");
    expect(optionValues).not.toContain("gpt-5-mini");
  });

  it("prefers the catalog provider when the active session reports a stale provider", () => {
    const { state } = createChatHeaderState({
      model: "deepseek-chat",
      modelProvider: "zai",
      models: createModelCatalog(DEEPSEEK_CHAT_MODEL),
    });

    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect?.value).toBe("deepseek/deepseek-chat");
  });

  it("falls back to the server-qualified session model when catalog lookup fails", () => {
    const { state } = createChatHeaderState({
      model: "gpt-5-mini",
      models: [],
    });

    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const modelSelect = container.querySelector<HTMLSelectElement>(
      'select[data-chat-model-select="true"]',
    );
    expect(modelSelect?.value).toBe("openai/gpt-5-mini");

    const optionValues = Array.from(modelSelect?.querySelectorAll("option") ?? []).map(
      (option) => option.value,
    );
    expect(optionValues).toContain("openai/gpt-5-mini");
    expect(optionValues).not.toContain("gpt-5-mini");
  });

  it("prefers the session label over displayName in the grouped chat session selector", () => {
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    state.sessionKey = "agent:main:subagent:4f2146de-887b-4176-9abe-91140082959b";
    state.settings.sessionKey = state.sessionKey;
    state.sessionsResult = {
      ts: 0,
      path: "",
      count: 1,
      defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: null },
      sessions: [
        {
          key: state.sessionKey,
          kind: "direct",
          updatedAt: null,
          label: "cron-config-check",
          displayName: "webchat:g-agent-main-subagent-4f2146de-887b-4176-9abe-91140082959b",
        },
      ],
    };
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const [sessionSelect] = Array.from(container.querySelectorAll<HTMLSelectElement>("select"));
    const labels = Array.from(sessionSelect?.querySelectorAll("option") ?? []).map((option) =>
      option.textContent?.trim(),
    );

    expect(labels).toContain("Subagent: cron-config-check");
    expect(labels).not.toContain(state.sessionKey);
    expect(labels).not.toContain(
      "subagent:4f2146de-887b-4176-9abe-91140082959b · webchat:g-agent-main-subagent-4f2146de-887b-4176-9abe-91140082959b",
    );
  });

  it("keeps a unique scoped fallback when the current grouped session is missing from sessions.list", () => {
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    state.sessionKey = "agent:main:subagent:4f2146de-887b-4176-9abe-91140082959b";
    state.settings.sessionKey = state.sessionKey;
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const [sessionSelect] = Array.from(container.querySelectorAll<HTMLSelectElement>("select"));
    const labels = Array.from(sessionSelect?.querySelectorAll("option") ?? []).map((option) =>
      option.textContent?.trim(),
    );

    expect(labels).toContain("subagent:4f2146de-887b-4176-9abe-91140082959b");
    expect(labels).not.toContain("Subagent:");
  });

  it("keeps a unique scoped fallback when a grouped session row has no label or displayName", () => {
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    state.sessionKey = "agent:main:subagent:4f2146de-887b-4176-9abe-91140082959b";
    state.settings.sessionKey = state.sessionKey;
    state.sessionsResult = {
      ts: 0,
      path: "",
      count: 1,
      defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: null },
      sessions: [
        {
          key: state.sessionKey,
          kind: "direct",
          updatedAt: null,
        },
      ],
    };
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const [sessionSelect] = Array.from(container.querySelectorAll<HTMLSelectElement>("select"));
    const labels = Array.from(sessionSelect?.querySelectorAll("option") ?? []).map((option) =>
      option.textContent?.trim(),
    );

    expect(labels).toContain("subagent:4f2146de-887b-4176-9abe-91140082959b");
    expect(labels).not.toContain("Subagent:");
  });

  it("disambiguates duplicate grouped labels with the scoped key suffix", () => {
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    state.sessionKey = "agent:main:subagent:4f2146de-887b-4176-9abe-91140082959b";
    state.settings.sessionKey = state.sessionKey;
    state.sessionsResult = {
      ts: 0,
      path: "",
      count: 2,
      defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: null },
      sessions: [
        {
          key: "agent:main:subagent:4f2146de-887b-4176-9abe-91140082959b",
          kind: "direct",
          updatedAt: null,
          label: "cron-config-check",
        },
        {
          key: "agent:main:subagent:6fb8b84b-c31f-410f-b7df-1553c82e43c9",
          kind: "direct",
          updatedAt: null,
          label: "cron-config-check",
        },
      ],
    };
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const [sessionSelect] = Array.from(container.querySelectorAll<HTMLSelectElement>("select"));
    const labels = Array.from(sessionSelect?.querySelectorAll("option") ?? []).map((option) =>
      option.textContent?.trim(),
    );

    expect(labels).toContain(
      "Subagent: cron-config-check · subagent:4f2146de-887b-4176-9abe-91140082959b",
    );
    expect(labels).toContain(
      "Subagent: cron-config-check · subagent:6fb8b84b-c31f-410f-b7df-1553c82e43c9",
    );
    expect(labels).not.toContain("Subagent: cron-config-check");
  });

  it("prefixes duplicate agent session labels with the agent name", () => {
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    state.sessionKey = "agent:alpha:main";
    state.settings.sessionKey = state.sessionKey;
    state.agentsList = {
      defaultId: "alpha",
      mainKey: "agent:alpha:main",
      scope: "all",
      agents: [
        { id: "alpha", name: "Deep Chat" },
        { id: "beta", name: "Coding" },
      ],
    };
    state.sessionsResult = {
      ts: 0,
      path: "",
      count: 2,
      defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: null },
      sessions: [
        {
          key: "agent:alpha:main",
          kind: "direct",
          updatedAt: null,
        },
        {
          key: "agent:beta:main",
          kind: "direct",
          updatedAt: null,
        },
      ],
    };
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const [sessionSelect] = Array.from(container.querySelectorAll<HTMLSelectElement>("select"));
    const labels = Array.from(sessionSelect?.querySelectorAll("option") ?? []).map((option) =>
      option.textContent?.trim(),
    );

    expect(labels).toContain("Deep Chat (alpha) / main");
    expect(labels).toContain("Coding (beta) / main");
    expect(labels).not.toContain("main");
  });

  it("keeps agent-prefixed labels unique when a custom label already matches the prefix", () => {
    const { state } = createChatHeaderState({ omitSessionFromList: true });
    state.sessionKey = "agent:alpha:main";
    state.settings.sessionKey = state.sessionKey;
    state.agentsList = {
      defaultId: "alpha",
      mainKey: "agent:alpha:main",
      scope: "all",
      agents: [
        { id: "alpha", name: "Deep Chat" },
        { id: "beta", name: "Coding" },
      ],
    };
    state.sessionsResult = {
      ts: 0,
      path: "",
      count: 3,
      defaults: { modelProvider: "openai", model: "gpt-5", contextTokens: null },
      sessions: [
        {
          key: "agent:alpha:main",
          kind: "direct",
          updatedAt: null,
        },
        {
          key: "agent:beta:main",
          kind: "direct",
          updatedAt: null,
        },
        {
          key: "agent:alpha:named-main",
          kind: "direct",
          updatedAt: null,
          label: "Deep Chat (alpha) / main",
        },
      ],
    };
    const container = document.createElement("div");
    render(renderChatSessionSelect(state), container);

    const [sessionSelect] = Array.from(container.querySelectorAll<HTMLSelectElement>("select"));
    const labels = Array.from(sessionSelect?.querySelectorAll("option") ?? []).map((option) =>
      option.textContent?.trim(),
    );

    expect(labels.filter((label) => label === "Deep Chat (alpha) / main")).toHaveLength(1);
    expect(labels).toContain("Deep Chat (alpha) / main · named-main");
    expect(labels).toContain("Coding (beta) / main");
  });
});
