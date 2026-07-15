/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { createStorageMock } from "../../test-helpers/storage.ts";
import { stashControlUiLaunchTicket } from "./control-ui-launch.ts";
import {
  applyWorkspaceFilesSettingsFromUrl,
  deleteWorkspaceFile,
  loadWorkspaceFiles,
  resolveControlUiApiBaseUrl,
  resolveWorkspaceFilesApiBaseUrl,
  uploadWorkspaceFiles,
  type WorkspaceFilesState,
} from "./workspace-files.ts";

function createState(): WorkspaceFilesState {
  return {
    workspaceFilesLoading: false,
    workspaceFilesError: null,
    workspaceFilesList: null,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function browserOrigin(): string {
  return window.location.origin === "null" ? "http://localhost" : window.location.origin;
}

describe("workspace-files controller", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: createStorageMock(),
    });
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: createStorageMock(),
    });
    window.history.replaceState({}, "", "/chat?session=main");
  });

  it("imports a fragment api url, persists it, and strips it from the URL", () => {
    window.history.replaceState(
      {},
      "",
      "/chat?session=main#ironAgentApiUrl=http://127.0.0.1:8000/&token=gateway-token",
    );

    const resolved = resolveWorkspaceFilesApiBaseUrl();

    expect(resolved).toBe(browserOrigin());
    expect(localStorage.getItem("ironagent.ironagent.apiUrl")).toBe(browserOrigin());
    expect(sessionStorage.getItem("ironagent.ironagent.controlApiUrl.session")).toBe(
      "http://127.0.0.1:8000",
    );
    expect(window.location.search).toBe("?session=main");
    expect(window.location.hash).toBe("#token=gateway-token");
  });

  it("preserves the explicit control api url for launch ticket resolution", () => {
    window.history.replaceState(
      {},
      "",
      "/chat?session=main#ironAgentApiUrl=http://127.0.0.1:19000/&launchTicket=launch-1",
    );

    const resolved = resolveControlUiApiBaseUrl();

    expect(resolved).toBe("http://127.0.0.1:19000");
    expect(localStorage.getItem("ironagent.ironagent.apiUrl")).toBe(browserOrigin());
    expect(sessionStorage.getItem("ironagent.ironagent.controlApiUrl.session")).toBe(
      "http://127.0.0.1:19000",
    );
    expect(window.location.search).toBe("?session=main");
    expect(window.location.hash).toBe("#launchTicket=launch-1");
  });

  it("keeps the control api url after workspace settings scrub launch params", () => {
    window.history.replaceState(
      {},
      "",
      "/chat?session=main#ironAgentApiUrl=http://127.0.0.1:19000/&launchTicket=launch-1",
    );

    applyWorkspaceFilesSettingsFromUrl();

    expect(resolveControlUiApiBaseUrl()).toBe("http://127.0.0.1:19000");
    expect(resolveWorkspaceFilesApiBaseUrl()).toBe(browserOrigin());
    expect(window.location.hash).toBe("#launchTicket=launch-1");
  });

  it("normalizes private bridge api urls to the current local browser host", () => {
    window.history.replaceState(
      {},
      "",
      "/chat?session=main#ironagentApiUrl=http://172.28.135.254:19000&token=gateway-token",
    );

    const resolved = resolveWorkspaceFilesApiBaseUrl();

    expect(resolved).toBe(browserOrigin());
    expect(localStorage.getItem("ironagent.ironagent.apiUrl")).toBe(browserOrigin());
    expect(window.location.search).toBe("?session=main");
    expect(window.location.hash).toBe("#token=gateway-token");
  });

  it("imports admin token from the URL fragment into session storage and strips it", () => {
    window.history.replaceState(
      {},
      "",
      "/chat?session=main#ironagentAdminToken=token-123&token=gateway-token",
    );

    applyWorkspaceFilesSettingsFromUrl();

    expect(sessionStorage.getItem("ironagent.ironagent.adminToken.session")).toBe("token-123");
    expect(localStorage.getItem("ironagent.ironagent.adminToken")).toBeNull();
    expect(window.location.search).toBe("?session=main");
    expect(window.location.hash).toBe("#token=gateway-token");
  });

  it("imports assistant id from the URL fragment into storage and strips it", () => {
    window.history.replaceState(
      {},
      "",
      "/chat?session=main#ironagentAssistantId=finance-check-123&token=gateway-token",
    );

    applyWorkspaceFilesSettingsFromUrl();

    expect(sessionStorage.getItem("ironagent.ironagent.assistantId.session")).toBe(
      "finance-check-123",
    );
    expect(localStorage.getItem("ironagent.ironagent.assistantId")).toBe("finance-check-123");
    expect(window.location.search).toBe("?session=main");
    expect(window.location.hash).toBe("#token=gateway-token");
  });

  it("scrubs legacy admin and API URL params without storing them on single-entry routes", () => {
    window.history.replaceState(
      {},
      "",
      "/assistants/demo-main/ui/chat?session=main#ironagentApiUrl=http://127.0.0.1:19000&ironagentAdminToken=admin-token&token=gateway-token",
    );

    applyWorkspaceFilesSettingsFromUrl();

    expect(localStorage.getItem("ironagent.ironagent.apiUrl")).toBeNull();
    expect(sessionStorage.getItem("ironagent.ironagent.adminToken.session")).toBeNull();
    expect(window.location.search).toBe("?session=main");
    expect(window.location.hash).toBe("#token=gateway-token");
  });

  it("loads workspace files and maps download links", async () => {
    const state = createState();
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        files: [
          {
            file_id: "file-1",
            assistant_id: "test001",
            bucket: "persistent",
            filename: "report.xlsx",
            relative_path: "persistent/report.xlsx",
            size_bytes: 12,
            content_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source_type: "workspace-scan",
            source_ref: "assistant-output",
            updated_at: "2026-04-13T12:00:00Z",
            deleted: false,
          },
          {
            file_id: "file-skill",
            assistant_id: "test001",
            bucket: "skills",
            filename: "SKILL.md",
            relative_path: "skills/demo/SKILL.md",
            size_bytes: 20,
            content_type: "text/markdown",
            source_type: "workspace-scan",
            source_ref: "assistant-output",
            updated_at: "2026-04-13T12:01:00Z",
            deleted: false,
          },
        ],
        total: 2,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "test001");

    expect(fetchMock).toHaveBeenCalledWith(
      `${browserOrigin()}/api/v1/workspace-files?assistant_id=test001`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(state.workspaceFilesError).toBeNull();
    expect(state.workspaceFilesLoading).toBe(false);
    expect(state.workspaceFilesList).toEqual({
      agentId: "test001",
      workspace: "test001 persistent workspace",
      files: [
        expect.objectContaining({
          fileId: "file-1",
          name: "report.xlsx",
          path: "persistent/report.xlsx",
          bucket: "persistent",
          viewUrl: `${browserOrigin()}/api/v1/workspace-files/file-1/download?disposition=inline`,
          downloadUrl: `${browserOrigin()}/api/v1/workspace-files/file-1/download?disposition=attachment`,
          sourceType: "workspace-scan",
          sourceRef: "assistant-output",
        }),
      ],
    });
  });

  it("keeps the existing list visible during a background workspace refresh", async () => {
    const state = createState();
    state.workspaceFilesList = {
      agentId: "test001",
      workspace: "test001 persistent workspace",
      files: [
        {
          fileId: "old-file",
          name: "old.txt",
          path: "persistent/old.txt",
          bucket: "persistent",
          size: 3,
          contentType: "text/plain",
          sourceType: "workspace-scan",
          sourceRef: "assistant-output",
          updatedAtMs: Date.parse("2026-04-13T12:00:00Z"),
          missing: false,
          viewUrl:
            "http://localhost:19000/api/v1/workspace-files/old-file/download?disposition=inline",
          downloadUrl:
            "http://localhost:19000/api/v1/workspace-files/old-file/download?disposition=attachment",
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        files: [
          {
            file_id: "new-file",
            assistant_id: "test001",
            bucket: "persistent",
            filename: "new.txt",
            relative_path: "persistent/new.txt",
            size_bytes: 4,
            content_type: "text/plain",
            source_type: "workspace-scan",
            source_ref: "assistant-output",
            updated_at: "2026-04-13T12:01:00Z",
            deleted: false,
          },
        ],
        total: 1,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const loadPromise = loadWorkspaceFiles(state, "test001", { background: true });

    expect(state.workspaceFilesLoading).toBe(false);
    expect(state.workspaceFilesList.files[0]?.fileId).toBe("old-file");
    await loadPromise;
    expect(state.workspaceFilesLoading).toBe(false);
    expect(state.workspaceFilesError).toBeNull();
    expect(state.workspaceFilesList.files[0]).toEqual(
      expect.objectContaining({
        fileId: "new-file",
        name: "new.txt",
      }),
    );
  });

  it("requests a workspace filesystem sync when asked", async () => {
    const state = createState();
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        files: [],
        total: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "test001", { sync: true });

    expect(fetchMock).toHaveBeenCalledWith(
      `${browserOrigin()}/api/v1/workspace-files?assistant_id=test001&sync=true`,
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("uses the assistant single-entry path for workspace requests", async () => {
    window.history.replaceState({}, "", "/assistants/demo-main/ui/chat?session=main");
    const state = createState();
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "main");

    expect(fetchMock).toHaveBeenCalledWith(
      "/assistants/demo-main/control/api/v1/workspace-files?assistant_id=demo-main",
      expect.objectContaining({ method: "GET" }),
    );
    expect(state.workspaceFilesList?.agentId).toBe("demo-main");
  });

  it("uploads workspace files through the assistant control path without browser tokens", async () => {
    window.history.replaceState({}, "", "/assistants/demo-main/ui/chat?session=main");
    const state = createState();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-2",
          assistant_id: "demo-main",
          filename: "evidence.pdf",
          bucket: "temp",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await uploadWorkspaceFiles(state, "main", [
      new File(["pdf"], "evidence.pdf", { type: "application/pdf" }),
    ]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/assistants/demo-main/control/api/v1/workspace-files/upload",
      expect.objectContaining({ method: "POST", headers: undefined }),
    );
    const firstBody = fetchMock.mock.calls[0]?.[1]?.body as FormData;
    expect(firstBody.get("assistant_id")).toBe("demo-main");
    expect(firstBody.get("target_bucket")).toBe("persistent");
    expect(state.workspaceFilesError).toBeNull();
  });

  it("deletes workspace files through the assistant single-entry path without browser tokens", async () => {
    window.history.replaceState({}, "", "/assistants/demo-main/ui/chat?session=main");
    const state = createState();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: "deleted", file_id: "file-1" }))
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await deleteWorkspaceFile(state, "main", "file-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/assistants/demo-main/control/api/v1/workspace-files/file-1/delete",
      expect.objectContaining({ method: "POST", headers: undefined }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/assistants/demo-main/control/api/v1/workspace-files?assistant_id=demo-main",
      expect.objectContaining({ method: "GET" }),
    );
    expect(state.workspaceFilesError).toBeNull();
  });

  it("returns pending approval when deleting persistent workspace files", async () => {
    window.history.replaceState({}, "", "/assistants/demo-main/ui/chat?session=main");
    const state = createState();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          status: "pending_approval",
          file_id: "file-1",
          assistant_id: "demo-main",
          relative_path: "persistent/report.pdf",
          bucket: "persistent",
          approval_id: "approval-1",
          message: "waiting",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await deleteWorkspaceFile(state, "main", "file-1");

    expect(result).toEqual({
      status: "pending_approval",
      fileId: "file-1",
      assistantId: "demo-main",
      path: "persistent/report.pdf",
      bucket: "persistent",
      approvalId: "approval-1",
      message: "waiting",
    });
    expect(state.workspaceFilesError).toBeNull();
  });

  it("marks workspace files missing when IronAgent reports missing_from_workspace", async () => {
    const state = createState();
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        files: [
          {
            file_id: "file-missing",
            assistant_id: "test001",
            bucket: "temp",
            filename: "11.txt",
            relative_path: "temp/11.txt",
            size_bytes: 12,
            content_type: "text/plain",
            source_type: "workspace-scan",
            source_ref: "assistant-output",
            sync_status: "missing_from_workspace",
            updated_at: "2026-04-27T12:00:00Z",
            deleted: false,
          },
        ],
        total: 1,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "test001");

    expect(state.workspaceFilesList?.files).toEqual([
      expect.objectContaining({
        fileId: "file-missing",
        path: "temp/11.txt",
        missing: true,
      }),
    ]);
  });

  it("uploads files, sends admin token header, and refreshes the list", async () => {
    const state = createState();
    localStorage.setItem("ironagent.ironagent.adminToken", "token-123");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-2",
          assistant_id: "test001",
          filename: "evidence.pdf",
          bucket: "persistent",
          relative_path: "persistent/evidence.pdf",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          files: [
            {
              file_id: "file-2",
              assistant_id: "test001",
              bucket: "persistent",
              filename: "evidence.pdf",
              relative_path: "persistent/evidence.pdf",
              size_bytes: 20,
              content_type: "application/pdf",
              source_type: "webui-chat",
              source_ref: "chat-send",
              updated_at: "2026-04-13T12:00:00Z",
              deleted: false,
            },
          ],
          total: 1,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const uploads = await uploadWorkspaceFiles(state, "test001", [
      new File(["pdf"], "evidence.pdf", { type: "application/pdf" }),
    ]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${browserOrigin()}/api/v1/workspace-files/upload`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "token-123",
          "X-Admin-Token": "token-123",
        },
        body: expect.any(FormData),
      }),
    );
    const firstBody = fetchMock.mock.calls[0]?.[1]?.body;
    expect(firstBody).toBeInstanceOf(FormData);
    expect((firstBody as FormData).get("assistant_id")).toBe("test001");
    expect((firstBody as FormData).get("target_bucket")).toBe("persistent");
    expect((firstBody as FormData).get("source_type")).toBe("webui-chat");
    expect((firstBody as FormData).get("source_ref")).toBe("chat-send");

    expect(uploads).toEqual([
      expect.objectContaining({
        status: "accepted",
        fileId: "file-2",
        assistantId: "test001",
        filename: "evidence.pdf",
        path: "persistent/evidence.pdf",
        downloadUrl: `${browserOrigin()}/api/v1/workspace-files/file-2/download?disposition=attachment`,
      }),
    ]);
    expect(state.workspaceFilesList?.files[0]).toEqual(
      expect.objectContaining({
        fileId: "file-2",
        name: "evidence.pdf",
        viewUrl: `${browserOrigin()}/api/v1/workspace-files/file-2/download?disposition=inline`,
        downloadUrl: `${browserOrigin()}/api/v1/workspace-files/file-2/download?disposition=attachment`,
        sourceType: "webui-chat",
      }),
    );
    expect(state.workspaceFilesLoading).toBe(false);
  });

  it("hydrates the admin token from a launch ticket before uploading", async () => {
    const state = createState();
    localStorage.setItem("ironagent.ironagent.apiUrl", "http://127.0.0.1:19000");
    stashControlUiLaunchTicket("launch-123");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          assistant_id: "test001",
          gateway_url: "ws://127.0.0.1:18789",
          gateway_token: "gateway-token",
          admin_token: "admin-from-launch",
          session_key: "agent:test001:main",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-2",
          assistant_id: "test001",
          filename: "evidence.xlsx",
          bucket: "persistent",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          files: [],
          total: 0,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await uploadWorkspaceFiles(state, "test001", [
      new File(["sheet"], "evidence.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
    ]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${browserOrigin()}/api/v1/control-ui/launch-tickets/resolve`,
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${browserOrigin()}/api/v1/workspace-files/upload`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "admin-from-launch",
          "X-Admin-Token": "admin-from-launch",
        },
        body: expect.any(FormData),
      }),
    );
    expect(sessionStorage.getItem("ironagent.ironagent.adminToken.session")).toBe(
      "admin-from-launch",
    );
    expect(sessionStorage.getItem("ironagent.ironagent.assistantId.session")).toBe("test001");
  });

  it("ignores placeholder admin tokens and refreshes from launch ticket before upload", async () => {
    const state = createState();
    sessionStorage.setItem("ironagent.ironagent.adminToken.session", "change-me");
    stashControlUiLaunchTicket("launch-placeholder");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          assistant_id: "test001",
          gateway_url: "ws://127.0.0.1:18789",
          gateway_token: "gateway-token",
          admin_token: "resolved-token",
          session_key: "agent:test001:main",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-10",
          assistant_id: "test001",
          filename: "placeholder.pdf",
          bucket: "persistent",
          relative_path: "persistent/placeholder.pdf",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await uploadWorkspaceFiles(state, "test001", [
      new File(["pdf"], "placeholder.pdf", { type: "application/pdf" }),
    ]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${browserOrigin()}/api/v1/control-ui/launch-tickets/resolve`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${browserOrigin()}/api/v1/workspace-files/upload`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "resolved-token",
          "X-Admin-Token": "resolved-token",
        },
      }),
    );
  });

  it("refreshes a stale admin token and retries workspace upload once", async () => {
    const state = createState();
    sessionStorage.setItem("ironagent.ironagent.adminToken.session", "stale-token");
    stashControlUiLaunchTicket("launch-456");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid admin token" }, 401))
      .mockResolvedValueOnce(
        jsonResponse({
          assistant_id: "test001",
          gateway_url: "ws://127.0.0.1:18789",
          gateway_token: "gateway-token",
          admin_token: "fresh-token",
          session_key: "agent:test001:main",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-9",
          assistant_id: "test001",
          filename: "renewed.pdf",
          bucket: "persistent",
          relative_path: "persistent/renewed.pdf",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    const uploads = await uploadWorkspaceFiles(state, "test001", [
      new File(["pdf"], "renewed.pdf", { type: "application/pdf" }),
    ]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${browserOrigin()}/api/v1/workspace-files/upload`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "stale-token",
          "X-Admin-Token": "stale-token",
        },
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${browserOrigin()}/api/v1/control-ui/launch-tickets/resolve`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `${browserOrigin()}/api/v1/workspace-files/upload`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "fresh-token",
          "X-Admin-Token": "fresh-token",
        },
      }),
    );
    expect(sessionStorage.getItem("ironagent.ironagent.adminToken.session")).toBe("fresh-token");
    expect(uploads).toEqual([
      expect.objectContaining({
        status: "accepted",
        fileId: "file-9",
        filename: "renewed.pdf",
      }),
    ]);
  });

  it("allows overriding workspace upload metadata", async () => {
    const state = createState();
    sessionStorage.setItem("ironagent.ironagent.adminToken.session", "token-123");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-3",
          assistant_id: "test001",
          filename: "notes.txt",
          bucket: "persistent",
          relative_path: "persistent/notes.txt",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await uploadWorkspaceFiles(
      state,
      "test001",
      [new File(["hello"], "notes.txt", { type: "text/plain" })],
      {
        targetBucket: "persistent",
        sourceType: "manual-upload",
        sourceRef: "workspace-tab",
      },
    );

    const firstBody = fetchMock.mock.calls[0]?.[1]?.body;
    expect((firstBody as FormData).get("target_bucket")).toBe("persistent");
    expect((firstBody as FormData).get("source_type")).toBe("manual-upload");
    expect((firstBody as FormData).get("source_ref")).toBe("workspace-tab");
  });

  it("self-heals stale local workspace api ports from storage", () => {
    localStorage.setItem("ironagent.ironagent.apiUrl", "http://127.0.0.1:19000");

    const resolved = resolveWorkspaceFilesApiBaseUrl();

    expect(resolved).toBe(browserOrigin());
    expect(localStorage.getItem("ironagent.ironagent.apiUrl")).toBe(browserOrigin());
  });

  it("uses the resolved control-ui assistant id for workspace file requests", async () => {
    const state = createState();
    sessionStorage.setItem("ironagent.ironagent.assistantId.session", "demo-main");
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        files: [],
        total: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "main");

    expect(fetchMock).toHaveBeenCalledWith(
      `${browserOrigin()}/api/v1/workspace-files?assistant_id=demo-main`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(state.workspaceFilesList?.agentId).toBe("demo-main");
  });

  it("refreshes a stale admin token and retries workspace delete once", async () => {
    const state = createState();
    sessionStorage.setItem("ironagent.ironagent.adminToken.session", "stale-token");
    stashControlUiLaunchTicket("launch-delete");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid admin token" }, 401))
      .mockResolvedValueOnce(
        jsonResponse({
          assistant_id: "demo-main",
          gateway_url: "ws://127.0.0.1:18789",
          gateway_token: "gateway-token",
          admin_token: "fresh-delete-token",
          session_key: "agent:demo-main:main",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          status: "deleted",
          file_id: "file-1",
          assistant_id: "demo-main",
          relative_path: "temp/report.txt",
          bucket: "temp",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ files: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await deleteWorkspaceFile(state, "demo-main", "file-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${browserOrigin()}/api/v1/workspace-files/file-1/delete`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "stale-token",
          "X-Admin-Token": "stale-token",
        },
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${browserOrigin()}/api/v1/control-ui/launch-tickets/resolve`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `${browserOrigin()}/api/v1/workspace-files/file-1/delete`,
      expect.objectContaining({
        method: "POST",
        headers: {
          IRONAGENT_ADMIN_TOKEN: "fresh-delete-token",
          "X-Admin-Token": "fresh-delete-token",
        },
      }),
    );
    expect(result).toEqual({
      status: "deleted",
      fileId: "file-1",
      assistantId: "demo-main",
      path: "temp/report.txt",
      bucket: "temp",
      approvalId: undefined,
      message: undefined,
    });
  });

  it("falls back to the only control-ui assistant when chat is opened as main without stored assistant context", async () => {
    const state = createState();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          assistants: [{ id: "demo-main", status: "running" }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          files: [
            {
              file_id: "file-3",
              assistant_id: "demo-main",
              bucket: "temp",
              filename: "3.txt",
              relative_path: "temp/3.txt",
              size_bytes: 14,
              content_type: "text/plain",
              source_type: "manual_import",
              source_ref: "legacy_object_storage",
              updated_at: "2026-04-14T08:59:35Z",
              deleted: false,
            },
          ],
          total: 1,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "main");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${browserOrigin()}/api/v1/control-ui/assistant-context`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${browserOrigin()}/api/v1/workspace-files?assistant_id=demo-main`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(localStorage.getItem("ironagent.ironagent.assistantId")).toBe("demo-main");
    expect(sessionStorage.getItem("ironagent.ironagent.assistantId.session")).toBe("demo-main");
    expect(state.workspaceFilesList?.agentId).toBe("demo-main");
    expect(state.workspaceFilesList?.files.map((file) => file.path)).toEqual(["temp/3.txt"]);
  });

  it("uses same-origin API paths for non-local browser sessions", () => {
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: new URL("https://control.example/chat?session=main") as unknown as Location,
    });
    try {
      expect(resolveWorkspaceFilesApiBaseUrl()).toBe("");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: originalLocation,
      });
    }
  });

  it("uploads files to the resolved control-ui assistant id", async () => {
    const state = createState();
    sessionStorage.setItem("ironagent.ironagent.assistantId.session", "demo-main");
    sessionStorage.setItem("ironagent.ironagent.adminToken.session", "token-123");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          status: "accepted",
          file_id: "file-2",
          assistant_id: "demo-main",
          filename: "evidence.pdf",
          bucket: "persistent",
          message: "ok",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          files: [],
          total: 0,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await uploadWorkspaceFiles(state, "main", [
      new File(["pdf"], "evidence.pdf", { type: "application/pdf" }),
    ]);

    const firstBody = fetchMock.mock.calls[0]?.[1]?.body as FormData;
    expect(firstBody.get("assistant_id")).toBe("demo-main");
  });

  it("fails with a clear error without replacing the workspace file list state", async () => {
    const state = createState();
    await expect(
      uploadWorkspaceFiles(state, "test001", [new File(["pdf"], "evidence.pdf")]),
    ).rejects.toThrow("当前页面缺少工作区上传凭证，请从后台重新打开聊天页。");

    expect(state.workspaceFilesError).toBeNull();
  });

  it("captures api errors when loading files fails", async () => {
    const state = createState();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "assistant not found" }, 404)),
    );

    await loadWorkspaceFiles(state, "missing-assistant");

    expect(state.workspaceFilesList).toBeNull();
    expect(state.workspaceFilesError).toContain("assistant not found");
    expect(state.workspaceFilesLoading).toBe(false);
  });

  it("retries one transient browser fetch failure before surfacing an error", async () => {
    const state = createState();
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(
        jsonResponse({
          files: [
            {
              file_id: "file-1",
              assistant_id: "demo-main",
              bucket: "temp",
              filename: "3.txt",
              relative_path: "temp/3.txt",
              size_bytes: 14,
              content_type: "text/plain",
              source_type: "manual_import",
              source_ref: "legacy_object_storage",
              updated_at: "2026-04-14T08:59:35Z",
              deleted: false,
            },
          ],
          total: 1,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await loadWorkspaceFiles(state, "demo-main");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(state.workspaceFilesError).toBeNull();
    expect(state.workspaceFilesList?.files.map((file) => file.path)).toEqual(["temp/3.txt"]);
  });
});
