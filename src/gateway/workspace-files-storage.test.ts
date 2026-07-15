import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearConfigCache } from "../config/config.js";
import type { WorkspaceObjectStore } from "./workspace-files-s3.runtime.js";
import {
  __testing,
  getWorkspaceFilesStorage,
  type WorkspaceFileRecord,
} from "./workspace-files-storage.js";

async function makeTempDir(prefix: string): Promise<string> {
  return await fs.mkdtemp(path.join(os.tmpdir(), prefix));
}

describe("workspace-files storage", () => {
  afterEach(async () => {
    __testing.resetSingletonForTests();
    __testing.resetDepsForTests();
    clearConfigCache();
    delete process.env.IRONAGENT_HOME;
    delete process.env.IRONAGENT_STATE_DIR;
    delete process.env.IRONAGENT_CONFIG_PATH;
    delete process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH;
  });

  it("stores metadata in sqlite and restores content from object storage when local file is missing", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-storage-");
    const dbPath = path.join(stateDir, "workspace-files.sqlite");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = dbPath;

    let storedBody = Buffer.alloc(0);
    const putObject: WorkspaceObjectStore["putObject"] = vi.fn(async ({ body }) => {
      storedBody = Buffer.from(body);
      return { etag: "etag-1" };
    });
    const getObject: WorkspaceObjectStore["getObject"] = vi.fn(async () => storedBody);
    const objectStore: WorkspaceObjectStore = {
      putObject,
      getObject,
      deleteObject: vi.fn(async () => {}),
    };

    __testing.setDepsForTests({
      now: () => new Date("2026-04-22T12:00:00.000Z"),
      resolveRuntimeConfig: () => ({ dbPath, objectStore }),
    });

    const storage = getWorkspaceFilesStorage();
    const created = await storage.createFile({
      assistantId: "main",
      bucket: "temp",
      filename: "report.txt",
      contentType: "text/plain",
      sourceType: "webui-chat",
      sourceRef: "chat-send:test",
      bytes: Buffer.from("hello workspace"),
    });

    expect(created).toMatchObject({
      assistant_id: "main",
      bucket: "temp",
      filename: "report.txt",
      content_type: "text/plain",
      source_type: "webui-chat",
      source_ref: "chat-send:test",
      updated_at: "2026-04-22T12:00:00.000Z",
      deleted: false,
    } satisfies Partial<WorkspaceFileRecord>);
    expect(created.relative_path).toMatch(/^temp\/report-[a-f0-9]{8}\.txt$/);
    expect(putObject).toHaveBeenCalledOnce();

    const listed = storage.listFiles("main");
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({
      file_id: created.file_id,
      relative_path: created.relative_path,
    });

    const firstRead = await storage.readFileBody(created.file_id);
    expect(firstRead?.body.toString("utf8")).toBe("hello workspace");

    const localPath = path.join(stateDir, ".ironagent", "workspace", created.relative_path);
    await fs.rm(localPath, { force: true });

    const fallbackRead = await storage.readFileBody(created.file_id);
    expect(getObject).toHaveBeenCalledOnce();
    expect(fallbackRead?.body.toString("utf8")).toBe("hello workspace");
    await expect(fs.readFile(localPath, "utf8")).resolves.toBe("hello workspace");
  });

  it("discovers files written directly into the assistant temp workspace via rebuildIndex", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-storage-");
    const dbPath = path.join(stateDir, "workspace-files.sqlite");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = dbPath;

    __testing.setDepsForTests({
      now: () => new Date("2026-04-22T12:00:00.000Z"),
      resolveRuntimeConfig: () => ({ dbPath, objectStore: null }),
    });

    const workspaceFile = path.join(stateDir, ".ironagent", "workspace", "temp", "10086.txt");
    await fs.mkdir(path.dirname(workspaceFile), { recursive: true });
    await fs.writeFile(workspaceFile, "20260512\n", "utf8");

    const storage = getWorkspaceFilesStorage();

    // listFiles reads only from SQLite — no workspace scan
    const beforeRebuild = storage.listFiles("main");
    expect(beforeRebuild).toHaveLength(0);

    // rebuildIndex scans the workspace directory into SQLite
    storage.rebuildIndex("main");

    const afterRebuild = storage.listFiles("main");
    expect(afterRebuild).toHaveLength(1);
    expect(afterRebuild[0]).toMatchObject({
      assistant_id: "main",
      bucket: "temp",
      filename: "10086.txt",
      relative_path: "temp/10086.txt",
      source_type: "workspace-scan",
      source_ref: "workspace-list",
      size_bytes: 9,
      deleted: false,
    });
  });

  it("reports created event to IronAgent index-events with correct payload", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-storage-");
    const dbPath = path.join(stateDir, "workspace-files.sqlite");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = dbPath;
    process.env.IRONAGENT_SERVICE_URL = "http://ironagent:8000";
    process.env.IRONAGENT_GATEWAY_TOKEN = "test-token";
    process.env.IRONAGENT_RUNTIME_ID = "test-runtime-001";

    let capturedBody: unknown = null;
    const mockFetch = vi.fn(async (_url: string, opts: RequestInit) => {
      capturedBody = JSON.parse(opts.body as string);
      return { ok: true, status: 200 } as Response;
    });
    vi.stubGlobal("fetch", mockFetch);

    __testing.setDepsForTests({
      now: () => new Date("2026-04-22T12:00:00.000Z"),
      resolveRuntimeConfig: () => ({
        dbPath,
        objectStore: null,
        ironagentServiceUrl: process.env.IRONAGENT_SERVICE_URL ?? "",
        ironagentGatewayToken: process.env.IRONAGENT_GATEWAY_TOKEN ?? "",
        ironagentRuntimeId: process.env.IRONAGENT_RUNTIME_ID ?? "",
      }),
    });

    const storage = getWorkspaceFilesStorage();
    await storage.createFile({
      assistantId: "main",
      bucket: "temp",
      filename: "report.txt",
      contentType: "text/plain",
      sourceType: "webui-chat",
      sourceRef: "chat-send:test",
      bytes: Buffer.from("hello"),
    });

    // Wait for async fire-and-forget report to settle
    await new Promise((r) => setTimeout(r, 10));

    expect(mockFetch).toHaveBeenCalledOnce();
    expect(capturedBody).not.toBeNull();
    const body = capturedBody as Record<string, unknown>;
    expect(body.assistant_id).toBe("main");
    expect(body.runtime_id).toBe("test-runtime-001");
    expect(body.events).toHaveLength(1);
    const event = (body.events as Record<string, unknown>[])[0];
    expect(event.action).toBe("created");
    expect(event.filename).not.toBe("report.txt"); // basename of relative_path, not raw filename
    expect(event.filename).toMatch(/^report-[a-f0-9]+\.txt$/);
    expect(event.content_type).toBe("text/plain");
    expect((event.size_bytes as number) > 0).toBe(true);

    vi.unstubAllGlobals();
  });

  it("does not block on IronAgent returning 403", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-storage-");
    const dbPath = path.join(stateDir, "workspace-files.sqlite");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = dbPath;
    process.env.IRONAGENT_SERVICE_URL = "http://ironagent:8000";
    process.env.IRONAGENT_GATEWAY_TOKEN = "test-token";
    process.env.IRONAGENT_RUNTIME_ID = "test-runtime-001";

    const mockFetch = vi.fn(async () => {
      return { ok: false, status: 403, statusText: "Forbidden" } as Response;
    });
    vi.stubGlobal("fetch", mockFetch);

    __testing.setDepsForTests({
      now: () => new Date("2026-04-22T12:00:00.000Z"),
      resolveRuntimeConfig: () => ({
        dbPath,
        objectStore: null,
        ironagentServiceUrl: process.env.IRONAGENT_SERVICE_URL ?? "",
        ironagentGatewayToken: process.env.IRONAGENT_GATEWAY_TOKEN ?? "",
        ironagentRuntimeId: process.env.IRONAGENT_RUNTIME_ID ?? "",
      }),
    });

    const storage = getWorkspaceFilesStorage();
    const result = await storage.createFile({
      assistantId: "main",
      bucket: "temp",
      filename: "report.txt",
      contentType: "text/plain",
      sourceType: "webui-chat",
      sourceRef: "chat-send:test",
      bytes: Buffer.from("hello"),
    });

    // File was created locally despite IronAgent 403
    expect(result).toBeDefined();
    expect(result.file_id).toBeTruthy();

    // Wait for async fire-and-forget report to settle
    await new Promise((r) => setTimeout(r, 10));
    expect(mockFetch).toHaveBeenCalledOnce();

    vi.unstubAllGlobals();
  });

  it("reports deleted event to IronAgent index-events", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-storage-");
    const dbPath = path.join(stateDir, "workspace-files.sqlite");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = dbPath;
    process.env.IRONAGENT_SERVICE_URL = "http://ironagent:8000";
    process.env.IRONAGENT_GATEWAY_TOKEN = "test-token";
    process.env.IRONAGENT_RUNTIME_ID = "test-runtime-001";

    let capturedBody: unknown = null;
    const mockFetch = vi.fn(async (_url: string, opts: RequestInit) => {
      capturedBody = JSON.parse(opts.body as string);
      return { ok: true, status: 200 } as Response;
    });
    vi.stubGlobal("fetch", mockFetch);

    __testing.setDepsForTests({
      now: () => new Date("2026-04-22T12:00:00.000Z"),
      resolveRuntimeConfig: () => ({
        dbPath,
        objectStore: null,
        ironagentServiceUrl: process.env.IRONAGENT_SERVICE_URL ?? "",
        ironagentGatewayToken: process.env.IRONAGENT_GATEWAY_TOKEN ?? "",
        ironagentRuntimeId: process.env.IRONAGENT_RUNTIME_ID ?? "",
      }),
    });

    const storage = getWorkspaceFilesStorage();
    const created = await storage.createFile({
      assistantId: "main",
      bucket: "temp",
      filename: "report.txt",
      contentType: "text/plain",
      sourceType: "webui-chat",
      sourceRef: "chat-send:test",
      bytes: Buffer.from("hello"),
    });
    // Wait for create fire-and-forget report
    await new Promise((r) => setTimeout(r, 10));
    // Reset capture after create
    capturedBody = null;
    mockFetch.mockClear();

    const deleted = await storage.deleteFile(created.file_id);
    expect(deleted).not.toBeNull();

    // Wait for delete fire-and-forget report
    await new Promise((r) => setTimeout(r, 10));
    expect(mockFetch).toHaveBeenCalledOnce();
    expect(capturedBody).not.toBeNull();
    const body = capturedBody as Record<string, unknown>;
    const event = (body.events as Record<string, unknown>[])[0];
    expect(event.action).toBe("deleted");
    expect(event.file_id).toBe(created.file_id);

    vi.unstubAllGlobals();
  });

  it("retries once on network error then succeeds", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-storage-");
    const dbPath = path.join(stateDir, "workspace-files.sqlite");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = dbPath;
    process.env.IRONAGENT_SERVICE_URL = "http://ironagent:8000";
    process.env.IRONAGENT_GATEWAY_TOKEN = "test-token";
    process.env.IRONAGENT_RUNTIME_ID = "test-runtime-001";

    let callCount = 0;
    const mockFetch = vi.fn(async () => {
      callCount++;
      // 1st call throws (network error), 2nd succeeds
      if (callCount === 1) {
        throw new Error("Network error (simulated)");
      }
      return { ok: true, status: 200 } as Response;
    });
    vi.stubGlobal("fetch", mockFetch);

    __testing.setDepsForTests({
      now: () => new Date("2026-04-22T12:00:00.000Z"),
      resolveRuntimeConfig: () => ({
        dbPath,
        objectStore: null,
        ironagentServiceUrl: process.env.IRONAGENT_SERVICE_URL ?? "",
        ironagentGatewayToken: process.env.IRONAGENT_GATEWAY_TOKEN ?? "",
        ironagentRuntimeId: process.env.IRONAGENT_RUNTIME_ID ?? "",
      }),
    });

    const storage = getWorkspaceFilesStorage();
    await storage.createFile({
      assistantId: "main",
      bucket: "temp",
      filename: "retry.txt",
      contentType: "text/plain",
      sourceType: "webui-chat",
      sourceRef: "chat-send:test",
      bytes: Buffer.from("retry-test"),
    });

    // Wait for async fire-and-forget report to settle
    await new Promise((r) => setTimeout(r, 1600));
    expect(mockFetch).toHaveBeenCalledTimes(2);

    vi.unstubAllGlobals();
  });
});
