import { beforeEach, describe, expect, it, vi } from "vitest";
import { createStorageMock } from "../test-helpers/storage.ts";

describe("device identity storage migration", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal("localStorage", createStorageMock());
    localStorage.clear();
  });

  it("migrates a legacy identity into the IronAgent namespace", async () => {
    localStorage.setItem(
      "ironagent-device-identity-v1",
      JSON.stringify({
        version: 1,
        deviceId: "bad-device-id",
        publicKey: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        privateKey: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        createdAtMs: 1,
      }),
    );

    const { loadOrCreateDeviceIdentity } = await import("./device-identity.ts");
    const identity = await loadOrCreateDeviceIdentity();

    expect(identity.deviceId).toBeTruthy();
    expect(identity.deviceId).not.toBe("bad-device-id");
    expect(localStorage.getItem("ironagent-device-identity-v1")).toBeTruthy();
  });

  it("writes both new and legacy identity keys while the migration window is open", async () => {
    const { loadOrCreateDeviceIdentity } = await import("./device-identity.ts");
    const identity = await loadOrCreateDeviceIdentity();

    const newStored = JSON.parse(localStorage.getItem("ironagent-device-identity-v1") ?? "{}");
    const legacyStored = JSON.parse(localStorage.getItem("ironagent-device-identity-v1") ?? "{}");

    expect(newStored.deviceId).toBe(identity.deviceId);
    expect(legacyStored.deviceId).toBe(identity.deviceId);
  });
});
