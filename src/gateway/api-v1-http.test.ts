import { afterEach, describe, expect, it } from "vitest";
import { completeTaskRunByRunId, createRunningTaskRun } from "../tasks/task-executor.js";
import { resetTaskRegistryForTests } from "../tasks/task-registry.js";
import { installGatewayTestHooks, withGatewayServer } from "./test-helpers.js";

installGatewayTestHooks({ scope: "suite" });

const API_TOKEN = "ironagent-secret";
const SERVER_OPTIONS = {
  auth: {
    mode: "token" as const,
    token: API_TOKEN,
  },
  controlUiEnabled: false,
};

function createAuthHeaders(): Record<string, string> {
  return {
    authorization: `Bearer ${API_TOKEN}`,
  };
}

afterEach(() => {
  resetTaskRegistryForTests({ persist: false });
});

describe("gateway /api/v1 HTTP adapter", () => {
  it("serves /api/v1/health without authentication", async () => {
    await withGatewayServer(
      async ({ port }) => {
        const response = await fetch(`http://127.0.0.1:${port}/api/v1/health`);

        expect(response.status).toBe(200);
        expect(await response.json()).toEqual({
          ok: true,
          status: "live",
        });
      },
      { serverOptions: SERVER_OPTIONS },
    );
  });

  it("returns the tools catalog for authenticated requests", async () => {
    await withGatewayServer(
      async ({ port }) => {
        const response = await fetch(
          `http://127.0.0.1:${port}/api/v1/tools/catalog?include_plugins=false`,
          {
            headers: createAuthHeaders(),
          },
        );

        expect(response.status).toBe(200);
        const payload = (await response.json()) as {
          ok?: boolean;
          agent_id?: string;
          groups?: Array<{
            id?: string;
            tools?: Array<{ id?: string }>;
          }>;
        };
        expect(payload.ok).toBe(true);
        expect(payload.agent_id).toBeTruthy();
        const mediaGroup = payload.groups?.find((group) => group.id === "media");
        expect(mediaGroup?.tools?.some((tool) => tool.id === "tts")).toBe(true);
      },
      { serverOptions: SERVER_OPTIONS },
    );
  });

  it("creates and deletes sessions via the REST adapter", async () => {
    await withGatewayServer(
      async ({ port }) => {
        const createResponse = await fetch(`http://127.0.0.1:${port}/api/v1/sessions/create`, {
          method: "POST",
          headers: {
            ...createAuthHeaders(),
            "content-type": "application/json",
          },
          body: JSON.stringify({
            label: "PandaIron Session",
          }),
        });

        expect(createResponse.status).toBe(200);
        const created = (await createResponse.json()) as {
          ok?: boolean;
          session_id?: string;
          session_key?: string;
        };
        expect(created.ok).toBe(true);
        expect(created.session_id).toBeTruthy();
        expect(created.session_key).toBeTruthy();

        const deleteResponse = await fetch(
          `http://127.0.0.1:${port}/api/v1/sessions/${encodeURIComponent(created.session_id ?? "")}`,
          {
            method: "DELETE",
            headers: createAuthHeaders(),
          },
        );

        expect(deleteResponse.status).toBe(200);
        expect(await deleteResponse.json()).toMatchObject({
          ok: true,
          deleted: true,
          session_key: created.session_key,
        });
      },
      { serverOptions: SERVER_OPTIONS },
    );
  });

  it("maps task registry states to pending and completed", async () => {
    await withGatewayServer(
      async ({ port }) => {
        createRunningTaskRun({
          runtime: "cli",
          ownerKey: "agent:main:main",
          scopeKind: "session",
          runId: "ironagent-run-1",
          task: "sync status",
          startedAt: Date.now(),
        });

        const pendingResponse = await fetch(
          `http://127.0.0.1:${port}/api/v1/tasks/ironagent-run-1`,
          {
            headers: createAuthHeaders(),
          },
        );
        expect(pendingResponse.status).toBe(200);
        expect(await pendingResponse.json()).toMatchObject({
          ok: true,
          task_id: expect.any(String),
          lookup_token: "ironagent-run-1",
          status: "pending",
          raw_status: "running",
        });

        completeTaskRunByRunId({
          runId: "ironagent-run-1",
          endedAt: Date.now(),
          terminalSummary: "done",
        });

        const completedResponse = await fetch(
          `http://127.0.0.1:${port}/api/v1/tasks/ironagent-run-1`,
          {
            headers: createAuthHeaders(),
          },
        );
        expect(completedResponse.status).toBe(200);
        expect(await completedResponse.json()).toMatchObject({
          ok: true,
          status: "completed",
          raw_status: "succeeded",
        });
      },
      { serverOptions: SERVER_OPTIONS },
    );
  });

  it("uploads and downloads files through /api/v1/files", async () => {
    await withGatewayServer(
      async ({ port }) => {
        const form = new FormData();
        form.set(
          "file",
          new File([Buffer.from("hello from ironagent", "utf-8")], "hello.txt", {
            type: "text/plain",
          }),
        );

        const uploadResponse = await fetch(`http://127.0.0.1:${port}/api/v1/files/upload`, {
          method: "POST",
          headers: createAuthHeaders(),
          body: form,
        });

        expect(uploadResponse.status).toBe(200);
        const uploaded = (await uploadResponse.json()) as {
          ok?: boolean;
          file_id?: string;
        };
        expect(uploaded.ok).toBe(true);
        expect(uploaded.file_id).toBeTruthy();

        const downloadResponse = await fetch(
          `http://127.0.0.1:${port}/api/v1/files/${encodeURIComponent(uploaded.file_id ?? "")}`,
          {
            headers: createAuthHeaders(),
          },
        );

        expect(downloadResponse.status).toBe(200);
        expect(downloadResponse.headers.get("content-type")).toContain("text/plain");
        expect(downloadResponse.headers.get("content-disposition")).toContain("hello.txt");
        expect(await downloadResponse.text()).toBe("hello from ironagent");
      },
      { serverOptions: SERVER_OPTIONS },
    );
  });
});
