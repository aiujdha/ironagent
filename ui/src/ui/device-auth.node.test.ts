import { beforeEach, describe, expect, it, vi } from "vitest";
import { createStorageMock } from "../test-helpers/storage.ts";

describe("device auth storage migration", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal("localStorage", createStorageMock());
    localStorage.clear();
  });

  it("migrates a legacy device auth store into the IronAgent namespace", async () => {
    localStorage.setItem(
      "ironagent.device.auth.v1",
      JSON.stringify({
        version: 1,
        deviceId: "device-1",
        tokens: {
          operator: {
            token: "legacy-token",
            role: "operator",
            scopes: ["operator:read"],
            updatedAtMs: 1,
          },
        },
      }),
    );

    const { loadDeviceAuthToken } = await import("./device-auth.ts");

    expect(loadDeviceAuthToken({ deviceId: "device-1", role: "operator" })?.token).toBe(
      "legacy-token",
    );
    expect(JSON.parse(localStorage.getItem("ironagent.device.auth.v1") ?? "{}")).toMatchObject({
      deviceId: "device-1",
      tokens: {
        operator: {
          token: "legacy-token",
        },
      },
    });
  });

  it("writes both new and legacy keys while the migration window is open", async () => {
    const { storeDeviceAuthToken } = await import("./device-auth.ts");

    storeDeviceAuthToken({
      deviceId: "device-2",
      role: "operator",
      token: "new-token",
      scopes: ["operator:read"],
    });

    expect(JSON.parse(localStorage.getItem("ironagent.device.auth.v1") ?? "{}")).toMatchObject({
      deviceId: "device-2",
      tokens: {
        operator: {
          token: "new-token",
        },
      },
    });
    expect(JSON.parse(localStorage.getItem("ironagent.device.auth.v1") ?? "{}")).toMatchObject({
      deviceId: "device-2",
      tokens: {
        operator: {
          token: "new-token",
        },
      },
    });
  });
});
