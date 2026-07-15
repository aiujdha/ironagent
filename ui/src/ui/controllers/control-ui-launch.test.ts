/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { createStorageMock } from "../../test-helpers/storage.ts";
import { resolveControlUiLaunchTicket, stashControlUiLaunchTicket } from "./control-ui-launch.ts";

describe("control-ui launch ticket controller", () => {
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
    localStorage.setItem("ironagent.ironagent.apiUrl", "http://127.0.0.1:19000");
  });

  it("returns none when no launch ticket is available", async () => {
    const result = await resolveControlUiLaunchTicket();

    expect(result).toEqual({ status: "none" });
  });

  it("resolves a stored launch ticket and hydrates the workspace admin token", async () => {
    stashControlUiLaunchTicket("launch-123");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            assistant_id: "demo-main",
            gateway_url: "ws://127.0.0.1:60599",
            gateway_token: "gateway-token",
            admin_token: "admin-token",
            session_key: "agent:main:main",
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    const result = await resolveControlUiLaunchTicket();

    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:19000/api/v1/control-ui/launch-tickets/resolve",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
    expect(result).toEqual({
      status: "resolved",
      assistantId: "demo-main",
      gatewayUrl: "ws://127.0.0.1:60599",
      gatewayToken: "gateway-token",
      sessionKey: "agent:main:main",
    });
    expect(sessionStorage.getItem("ironagent.control.launchTicket.v1")).toBeNull();
    expect(sessionStorage.getItem("ironagent.ironagent.adminToken.session")).toBe("admin-token");
    expect(sessionStorage.getItem("ironagent.ironagent.assistantId.session")).toBe("demo-main");
  });

  it("keeps launch resolution on same-origin single-entry routes", async () => {
    window.history.replaceState({}, "", "/assistants/demo-main/ui/chat?session=main");
    stashControlUiLaunchTicket("launch-123");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            assistant_id: "demo-main",
            gateway_url: "ws://127.0.0.1:60599",
            gateway_token: "runtime-token",
            admin_token: "admin-token",
            session_key: "agent:demo-main:main",
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    const result = await resolveControlUiLaunchTicket();

    expect(fetch).toHaveBeenCalledWith(
      "/assistants/demo-main/control/api/v1/control-ui/launch-tickets/resolve",
      expect.objectContaining({ method: "POST" }),
    );
    expect(result).toEqual({
      status: "resolved",
      assistantId: "demo-main",
      gatewayUrl: "ws://localhost:3000/assistants/demo-main/ws",
      gatewayToken: "",
      sessionKey: "agent:demo-main:main",
    });
    expect(sessionStorage.getItem("ironagent.ironagent.adminToken.session")).toBeNull();
  });

  it("clears an expired launch ticket and returns a friendly failure", async () => {
    stashControlUiLaunchTicket("launch-123");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "launch ticket expired" }), {
          status: 410,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const result = await resolveControlUiLaunchTicket();

    expect(result).toEqual({
      status: "failed",
      code: "control_ui_launch_failed",
      message: "安全入口已失效，请从后台重新打开聊天页。",
    });
    expect(sessionStorage.getItem("ironagent.control.launchTicket.v1")).toBeNull();
  });
});
