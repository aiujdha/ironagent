import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { clearConfigCache } from "../config/config.js";
import type { ResolvedGatewayAuth } from "./auth.js";
import { createGatewayHttpServer } from "./server-http.js";
import { __testing as workspaceFilesStorageTesting } from "./workspace-files-storage.js";

async function makeTempDir(prefix: string): Promise<string> {
  return await fs.mkdtemp(path.join(os.tmpdir(), prefix));
}

async function listen(server: ReturnType<typeof createGatewayHttpServer>) {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return {
    port,
    close: async () => {
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      );
    },
  };
}

function createWorkspaceServer(auth: ResolvedGatewayAuth) {
  return createGatewayHttpServer({
    canvasHost: null,
    clients: new Set(),
    controlUiEnabled: false,
    controlUiBasePath: "/__control__",
    openAiChatCompletionsEnabled: false,
    openResponsesEnabled: false,
    handleHooksRequest: async () => false,
    resolvedAuth: auth,
  });
}

describe("workspace-files HTTP API", () => {
  afterEach(async () => {
    workspaceFilesStorageTesting.resetSingletonForTests();
    workspaceFilesStorageTesting.resetDepsForTests();
    clearConfigCache();
    delete process.env.IRONAGENT_HOME;
    delete process.env.IRONAGENT_STATE_DIR;
    delete process.env.IRONAGENT_CONFIG_PATH;
    delete process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH;
  });

  it("uploads into temp, lists from sqlite metadata, and downloads file bytes", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-http-");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = path.join(stateDir, "workspace-files.sqlite");

    const server = createWorkspaceServer({
      mode: "token",
      token: "secret",
      password: undefined,
      allowTailscale: false,
    });
    const listener = await listen(server);
    try {
      const form = new FormData();
      form.append("file", new File(["hello pdf"], "evidence.pdf", { type: "application/pdf" }));
      form.append("assistant_id", "main");
      form.append("target_bucket", "temp");
      form.append("source_type", "webui-chat");
      form.append("source_ref", "chat-send:test");

      const uploadRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files/upload`,
        {
          method: "POST",
          headers: { "X-Admin-Token": "secret" },
          body: form,
        },
      );

      expect(uploadRes.status).toBe(200);
      const uploadJson = (await uploadRes.json()) as {
        status?: string;
        file_id?: string;
        relative_path?: string;
        bucket?: string;
      };
      expect(uploadJson.status).toBe("accepted");
      expect(uploadJson.bucket).toBe("temp");
      expect(uploadJson.relative_path).toMatch(/^temp\/evidence-[a-f0-9]{8}\.pdf$/);
      expect(typeof uploadJson.file_id).toBe("string");

      const listRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files?assistant_id=main`,
      );
      expect(listRes.status).toBe(200);
      const listJson = (await listRes.json()) as {
        files?: Array<{
          file_id?: string;
          relative_path?: string;
          bucket?: string;
          source_ref?: string;
        }>;
        total?: number;
      };
      expect(listJson.total).toBe(1);
      expect(listJson.files?.[0]).toMatchObject({
        file_id: uploadJson.file_id,
        relative_path: uploadJson.relative_path,
        bucket: "temp",
        source_ref: "chat-send:test",
      });

      const prefixedListRes = await fetch(
        `http://127.0.0.1:${listener.port}/assistants/main/api/v1/workspace-files?assistant_id=main`,
      );
      expect(prefixedListRes.status).toBe(200);

      const downloadRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files/${encodeURIComponent(
          String(uploadJson.file_id),
        )}/download?disposition=attachment`,
      );
      expect(downloadRes.status).toBe(200);
      expect(downloadRes.headers.get("content-type")).toBe("application/pdf");
      expect(downloadRes.headers.get("content-disposition")).toContain("attachment;");
      await expect(downloadRes.text()).resolves.toBe("hello pdf");

      const deleteRes = await fetch(
        `http://127.0.0.1:${listener.port}/assistants/main/api/v1/workspace-files/${encodeURIComponent(
          String(uploadJson.file_id),
        )}`,
        {
          method: "DELETE",
          headers: { "X-Admin-Token": "secret" },
        },
      );
      expect(deleteRes.status).toBe(200);

      const listAfterDeleteRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files?assistant_id=main`,
      );
      expect(listAfterDeleteRes.status).toBe(200);
      const listAfterDeleteJson = (await listAfterDeleteRes.json()) as { total?: number };
      expect(listAfterDeleteJson.total).toBe(0);

      const downloadAfterDeleteRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files/${encodeURIComponent(
          String(uploadJson.file_id),
        )}/download?disposition=attachment`,
      );
      expect(downloadAfterDeleteRes.status).toBe(404);
    } finally {
      await listener.close();
    }
  });

  it("requires approval before deleting persistent workspace files", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-http-");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = path.join(stateDir, "workspace-files.sqlite");

    const server = createWorkspaceServer({
      mode: "token",
      token: "secret",
      password: undefined,
      allowTailscale: false,
    });
    const listener = await listen(server);
    try {
      const form = new FormData();
      form.append("file", new File(["keep"], "policy.txt", { type: "text/plain" }));
      form.append("assistant_id", "main");
      form.append("target_bucket", "persistent");

      const uploadRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files/upload`,
        {
          method: "POST",
          headers: { "X-Admin-Token": "secret" },
          body: form,
        },
      );
      const uploadJson = (await uploadRes.json()) as {
        file_id?: string;
        relative_path?: string;
      };

      const deleteRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files/${encodeURIComponent(
          String(uploadJson.file_id),
        )}`,
        {
          method: "DELETE",
          headers: { "X-Admin-Token": "secret" },
        },
      );

      expect(deleteRes.status).toBe(202);
      const deleteJson = (await deleteRes.json()) as {
        status?: string;
        approval_id?: string;
        relative_path?: string;
        bucket?: string;
      };
      expect(deleteJson).toMatchObject({
        status: "pending_approval",
        relative_path: uploadJson.relative_path,
        bucket: "persistent",
      });
      expect(deleteJson.approval_id).toMatch(/^workspace-delete-/);

      const listAfterDeleteRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files?assistant_id=main`,
      );
      const listAfterDeleteJson = (await listAfterDeleteRes.json()) as { total?: number };
      expect(listAfterDeleteJson.total).toBe(1);
    } finally {
      await listener.close();
    }
  });

  it("indexes existing workspace files when sync is requested", async () => {
    const stateDir = await makeTempDir("ironagent-workspace-files-http-");
    process.env.IRONAGENT_HOME = stateDir;
    process.env.IRONAGENT_STATE_DIR = stateDir;
    process.env.IRONAGENT_WORKSPACE_FILES_DB_PATH = path.join(stateDir, "workspace-files.sqlite");
    const workspaceDir = path.join(stateDir, ".ironagent", "workspace", "temp");
    await fs.mkdir(workspaceDir, { recursive: true });
    await fs.writeFile(path.join(workspaceDir, "manual-sync.txt"), "hello sync");

    const server = createWorkspaceServer({
      mode: "token",
      token: "secret",
      password: undefined,
      allowTailscale: false,
    });
    const listener = await listen(server);
    try {
      const initialListRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files?assistant_id=main`,
      );
      expect(initialListRes.status).toBe(200);
      const initialListJson = (await initialListRes.json()) as { total?: number };
      expect(initialListJson.total).toBe(0);

      const syncListRes = await fetch(
        `http://127.0.0.1:${listener.port}/api/v1/workspace-files?assistant_id=main&sync=true`,
      );
      expect(syncListRes.status).toBe(200);
      const syncListJson = (await syncListRes.json()) as {
        files?: Array<{ relative_path?: string; source_type?: string }>;
        total?: number;
      };
      expect(syncListJson.total).toBe(1);
      expect(syncListJson.files?.[0]).toMatchObject({
        relative_path: "temp/manual-sync.txt",
        source_type: "workspace-scan",
      });
    } finally {
      await listener.close();
    }
  });
});
