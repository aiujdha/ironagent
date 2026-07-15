import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createStorageMock } from "../test-helpers/storage.ts";

function setTestLocation(params: { protocol: string; host: string; pathname: string }) {
  vi.stubGlobal("location", {
    protocol: params.protocol,
    host: params.host,
    hostname: params.host.replace(/:\d+$/, ""),
    pathname: params.pathname,
  } as Location);
}

function setControlUiBasePath(value: string | undefined) {
  if (typeof window === "undefined") {
    vi.stubGlobal(
      "window",
      value == null
        ? ({} as Window & typeof globalThis)
        : ({ __IRONAGENT_CONTROL_UI_BASE_PATH__: value } as Window & typeof globalThis),
    );
    return;
  }
  if (value == null) {
    delete window.__IRONAGENT_CONTROL_UI_BASE_PATH__;
    return;
  }
  Object.defineProperty(window, "__IRONAGENT_CONTROL_UI_BASE_PATH__", {
    value,
    writable: true,
    configurable: true,
  });
}

function expectedGatewayUrl(basePath: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${basePath}`;
}

describe("loadSettings default gateway URL derivation", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal("localStorage", createStorageMock());
    vi.stubGlobal("sessionStorage", createStorageMock());
    vi.stubGlobal("navigator", { language: "en-US" } as Navigator);
    localStorage.clear();
    sessionStorage.clear();
    setControlUiBasePath(undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    setControlUiBasePath(undefined);
    vi.unstubAllGlobals();
  });

  it("uses configured base path and normalizes trailing slash", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/ignored/path",
    });
    setControlUiBasePath(" /ironagent/ ");

    const { loadSettings } = await import("./storage.ts");
    expect(loadSettings().gatewayUrl).toBe(expectedGatewayUrl("/ironagent"));
  });

  it("infers base path from nested pathname when configured base path is not set", async () => {
    setTestLocation({
      protocol: "http:",
      host: "gateway.example:18789",
      pathname: "/apps/ironagent/chat",
    });

    const { loadSettings } = await import("./storage.ts");
    expect(loadSettings().gatewayUrl).toBe(expectedGatewayUrl("/apps/ironagent"));
  });

  it("skips node sessionStorage accessors that warn without a storage file", async () => {
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.stubGlobal("localStorage", createStorageMock());
    vi.stubGlobal("navigator", { language: "en-US" } as Navigator);
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });
    setControlUiBasePath(undefined);
    const warningSpy = vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);

    const { loadSettings } = await import("./storage.ts");

    expect(loadSettings()).toMatchObject({
      gatewayUrl: expectedGatewayUrl(""),
      token: "",
    });
    expect(warningSpy).not.toHaveBeenCalledWith(
      "`--localstorage-file` was provided without a valid path",
      expect.anything(),
      expect.anything(),
    );
  });

  it("ignores and scrubs legacy persisted tokens", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });
    sessionStorage.setItem("ironagent.control.token.v1", "legacy-session-token");
    localStorage.setItem(
      "ironagent.control.settings.v1",
      JSON.stringify({
        gatewayUrl: "wss://gateway.example:8443/ironagent",
        token: "persisted-token",
        sessionKey: "agent",
      }),
    );

    const { loadSettings } = await import("./storage.ts");
    expect(loadSettings()).toMatchObject({
      gatewayUrl: "wss://gateway.example:8443/ironagent",
      token: "",
      sessionKey: "agent",
    });
    const scopedKey = "ironagent.control.settings.v1:wss://gateway.example:8443/ironagent";
    const legacyScopedKey = "ironagent.control.settings.v1:wss://gateway.example:8443/ironagent";
    const expectedPersisted = {
      gatewayUrl: "wss://gateway.example:8443/ironagent",
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
      sessionsByGateway: {
        "wss://gateway.example:8443/ironagent": {
          sessionKey: "agent",
          lastActiveSessionKey: "agent",
        },
      },
    };
    expect(JSON.parse(localStorage.getItem(scopedKey) ?? "{}")).toMatchObject(expectedPersisted);
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}")).toMatchObject(
      expectedPersisted,
    );
    expect(JSON.parse(localStorage.getItem(legacyScopedKey) ?? "{}")).toMatchObject(
      expectedPersisted,
    );
    expect(sessionStorage.getItem("ironagent.control.token.v1")).toBeNull();
  });

  it("loads the current-tab token from sessionStorage", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    const { loadSettings, saveSettings } = await import("./storage.ts");
    saveSettings({
      gatewayUrl: gwUrl,
      token: "session-token",
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
    });

    expect(loadSettings()).toMatchObject({
      gatewayUrl: gwUrl,
      token: "session-token",
    });
  });

  it("does not reuse a session token for a different gatewayUrl", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    const otherUrl = "wss://other-gateway.example:8443";
    const { loadSettings, saveSettings } = await import("./storage.ts");
    saveSettings({
      gatewayUrl: gwUrl,
      token: "gateway-a-token",
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
    });

    saveSettings({
      gatewayUrl: otherUrl,
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
    });

    expect(loadSettings()).toMatchObject({
      gatewayUrl: gwUrl,
      token: "gateway-a-token",
    });
  });

  it("does not persist gateway tokens when saving settings", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    const { loadSettings, saveSettings } = await import("./storage.ts");
    saveSettings({
      gatewayUrl: gwUrl,
      token: "memory-only-token",
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
    });
    expect(loadSettings()).toMatchObject({
      gatewayUrl: gwUrl,
      token: "memory-only-token",
    });

    const scopedKey = `ironagent.control.settings.v1:${gwUrl}`;
    const legacyScopedKey = `ironagent.control.settings.v1:${gwUrl}`;
    const expectedPersisted = {
      gatewayUrl: gwUrl,
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
      sessionsByGateway: {
        [gwUrl]: {
          sessionKey: "main",
          lastActiveSessionKey: "main",
        },
      },
    };
    expect(JSON.parse(localStorage.getItem(scopedKey) ?? "{}")).toMatchObject(expectedPersisted);
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}")).toMatchObject(
      expectedPersisted,
    );
    expect(JSON.parse(localStorage.getItem(legacyScopedKey) ?? "{}")).toMatchObject(
      expectedPersisted,
    );
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}")).toMatchObject(
      expectedPersisted,
    );
    expect(sessionStorage.getItem(`ironagent.control.token.v1:${gwUrl}`)).toBe("memory-only-token");
    expect(sessionStorage.getItem(`ironagent.control.token.v1:${gwUrl}`)).toBe("memory-only-token");
    expect(sessionStorage.length).toBe(2);
  });

  it("clears the current-tab token when saving an empty token", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    const { loadSettings, saveSettings } = await import("./storage.ts");
    saveSettings({
      gatewayUrl: gwUrl,
      token: "stale-token",
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
    });
    saveSettings({
      gatewayUrl: gwUrl,
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
    });

    expect(loadSettings().token).toBe("");
    expect(sessionStorage.length).toBe(0);
  });

  it("persists themeMode and navWidth alongside the selected theme", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    const { saveSettings } = await import("./storage.ts");
    saveSettings({
      gatewayUrl: gwUrl,
      token: "",
      sessionKey: "main",
      lastActiveSessionKey: "main",
      theme: "dash",
      themeMode: "light",
      chatFocusMode: false,
      chatShowThinking: true,
      chatShowToolCalls: true,
      splitRatio: 0.6,
      navCollapsed: false,
      navWidth: 320,
      navGroupsCollapsed: {},
      borderRadius: 50,
    });

    const scopedKey = `ironagent.control.settings.v1:${gwUrl}`;
    expect(JSON.parse(localStorage.getItem(scopedKey) ?? "{}")).toMatchObject({
      theme: "dash",
      themeMode: "light",
      navWidth: 320,
    });
  });

  it("scopes persisted session selection per gateway", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway-a.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    const { loadSettings, saveSettings } = await import("./storage.ts");

    saveSettings({
      gatewayUrl: gwUrl,
      token: "",
      sessionKey: "agent:test_old:main",
      lastActiveSessionKey: "agent:test_old:main",
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
    });

    expect(loadSettings()).toMatchObject({
      gatewayUrl: gwUrl,
      sessionKey: "agent:test_old:main",
      lastActiveSessionKey: "agent:test_old:main",
    });
  });

  it("caps persisted session scopes to the most recent gateways", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const { saveSettings } = await import("./storage.ts");
    const gwUrl = expectedGatewayUrl("");
    const scopedKey = `ironagent.control.settings.v1:wss://gateway.example:8443`;

    // Pre-seed sessionsByGateway with 11 stale gateway entries so the next
    // saveSettings call pushes the total to 12 and triggers the cap (10).
    const staleEntries: Record<string, { sessionKey: string; lastActiveSessionKey: string }> = {};
    for (let i = 0; i < 11; i += 1) {
      staleEntries[`wss://stale-${i}.example:8443`] = {
        sessionKey: `agent:stale_${i}:main`,
        lastActiveSessionKey: `agent:stale_${i}:main`,
      };
    }
    localStorage.setItem(scopedKey, JSON.stringify({ sessionsByGateway: staleEntries }));

    saveSettings({
      gatewayUrl: gwUrl,
      token: "",
      sessionKey: "agent:current:main",
      lastActiveSessionKey: "agent:current:main",
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
    });

    const persisted = JSON.parse(localStorage.getItem(scopedKey) ?? "{}");

    expect(persisted.sessionsByGateway).toBeDefined();
    const scopes = Object.keys(persisted.sessionsByGateway);
    expect(scopes).toHaveLength(10);
    // oldest stale entries should be evicted
    expect(scopes).not.toContain("wss://stale-0.example:8443");
    expect(scopes).not.toContain("wss://stale-1.example:8443");
    // newest stale entries and the current gateway should be retained
    expect(scopes).toContain("wss://stale-10.example:8443");
    expect(scopes).toContain("wss://gateway.example:8443");
    expect(persisted.sessionsByGateway["wss://gateway.example:8443"]).toEqual({
      sessionKey: "agent:current:main",
      lastActiveSessionKey: "agent:current:main",
    });
  });

  it("migrates legacy scoped session tokens into the new namespace", async () => {
    setTestLocation({
      protocol: "https:",
      host: "gateway.example:8443",
      pathname: "/",
    });

    const gwUrl = expectedGatewayUrl("");
    sessionStorage.setItem(`ironagent.control.token.v1:${gwUrl}`, "legacy-scoped-token");

    const { loadSettings } = await import("./storage.ts");

    expect(loadSettings()).toMatchObject({
      gatewayUrl: gwUrl,
      token: "legacy-scoped-token",
    });
    expect(sessionStorage.getItem(`ironagent.control.token.v1:${gwUrl}`)).toBe(
      "legacy-scoped-token",
    );
  });
});
