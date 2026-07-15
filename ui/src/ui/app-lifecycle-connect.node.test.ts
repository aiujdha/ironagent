import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  applySettingsFromUrlMock,
  applySettingsMock,
  connectGatewayMock,
  loadBootstrapMock,
  resolveLaunchMock,
} = vi.hoisted(() => ({
  applySettingsFromUrlMock: vi.fn(),
  applySettingsMock: vi.fn(),
  connectGatewayMock: vi.fn(),
  loadBootstrapMock: vi.fn(),
  resolveLaunchMock: vi.fn(),
}));

vi.mock("./app-gateway.ts", () => ({
  connectGateway: connectGatewayMock,
}));

vi.mock("./controllers/control-ui-bootstrap.ts", () => ({
  loadControlUiBootstrapConfig: loadBootstrapMock,
}));

vi.mock("./controllers/control-ui-launch.ts", () => ({
  resolveControlUiLaunchTicket: resolveLaunchMock,
}));

vi.mock("./app-settings.ts", () => ({
  applySettings: applySettingsMock,
  applySettingsFromUrl: applySettingsFromUrlMock,
  attachThemeListener: vi.fn(),
  detachThemeListener: vi.fn(),
  inferBasePath: vi.fn(() => "/"),
  syncTabWithLocation: vi.fn(),
  syncThemeWithSettings: vi.fn(),
}));

vi.mock("./app-polling.ts", () => ({
  startLogsPolling: vi.fn(),
  startNodesPolling: vi.fn(),
  stopLogsPolling: vi.fn(),
  stopNodesPolling: vi.fn(),
  startDebugPolling: vi.fn(),
  stopDebugPolling: vi.fn(),
  startWorkspaceFilesPolling: vi.fn(),
  stopWorkspaceFilesPolling: vi.fn(),
}));

vi.mock("./app-scroll.ts", () => ({
  observeTopbar: vi.fn(),
  scheduleChatScroll: vi.fn(),
  scheduleLogsScroll: vi.fn(),
}));

import { handleConnected } from "./app-lifecycle.ts";

function createHost() {
  return {
    basePath: "",
    client: null,
    connectGeneration: 0,
    connected: false,
    settings: {
      gatewayUrl: "ws://gateway-a.example:18789",
      token: "",
      sessionKey: "main",
      lastActiveSessionKey: "main",
    },
    tab: "chat",
    assistantName: "IronAgent",
    assistantAvatar: null,
    assistantAgentId: null,
    serverVersion: null,
    chatHasAutoScrolled: false,
    chatManualRefreshInFlight: false,
    chatLoading: false,
    chatMessages: [],
    chatToolMessages: [],
    chatStream: "",
    logsAutoFollow: false,
    logsAtBottom: true,
    logsEntries: [],
    popStateHandler: vi.fn(),
    topbarObserver: null,
  };
}

async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
}

describe("handleConnected", () => {
  beforeEach(() => {
    applySettingsFromUrlMock.mockReset();
    applySettingsMock.mockReset();
    connectGatewayMock.mockReset();
    loadBootstrapMock.mockReset();
    resolveLaunchMock.mockReset();
    resolveLaunchMock.mockResolvedValue({ status: "none" });
  });

  it("waits for bootstrap load before first gateway connect", async () => {
    let resolveBootstrap!: () => void;
    loadBootstrapMock.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        resolveBootstrap = resolve;
      }),
    );
    resolveLaunchMock.mockResolvedValueOnce({ status: "none" });
    connectGatewayMock.mockReset();
    const host = createHost();

    handleConnected(host as never);
    expect(connectGatewayMock).not.toHaveBeenCalled();

    resolveBootstrap();
    await flushMicrotasks();
    expect(connectGatewayMock).toHaveBeenCalledTimes(1);
  });

  it("skips deferred connect when disconnected before bootstrap resolves", async () => {
    let resolveBootstrap!: () => void;
    loadBootstrapMock.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        resolveBootstrap = resolve;
      }),
    );
    resolveLaunchMock.mockResolvedValueOnce({ status: "none" });
    connectGatewayMock.mockReset();
    const host = createHost();

    handleConnected(host as never);
    expect(connectGatewayMock).not.toHaveBeenCalled();

    host.connectGeneration += 1;
    resolveBootstrap();
    await flushMicrotasks();

    expect(connectGatewayMock).not.toHaveBeenCalled();
  });

  it("scrubs URL settings before starting the bootstrap fetch", () => {
    loadBootstrapMock.mockResolvedValueOnce(undefined);
    const host = createHost();

    handleConnected(host as never);

    expect(applySettingsFromUrlMock).toHaveBeenCalledTimes(1);
    expect(loadBootstrapMock).toHaveBeenCalledTimes(1);
    expect(applySettingsFromUrlMock.mock.invocationCallOrder[0]).toBeLessThan(
      loadBootstrapMock.mock.invocationCallOrder[0],
    );
  });

  it("applies resolved launch-ticket settings before connecting", async () => {
    loadBootstrapMock.mockResolvedValueOnce(undefined);
    resolveLaunchMock.mockResolvedValueOnce({
      status: "resolved",
      gatewayUrl: "ws://gateway-b.example:18789",
      gatewayToken: "gateway-token",
      sessionKey: "agent:main:main",
    });
    const host = createHost();

    handleConnected(host as never);
    await flushMicrotasks();

    expect(applySettingsMock).toHaveBeenCalledWith(
      host,
      expect.objectContaining({
        gatewayUrl: "ws://gateway-b.example:18789",
        token: "gateway-token",
        sessionKey: "agent:main:main",
        lastActiveSessionKey: "agent:main:main",
      }),
    );
    expect(connectGatewayMock).toHaveBeenCalledTimes(1);
  });

  it("skips the initial connect when launch-ticket resolution fails and no token is available", async () => {
    loadBootstrapMock.mockResolvedValueOnce(undefined);
    resolveLaunchMock.mockResolvedValueOnce({
      status: "failed",
      code: "control_ui_launch_failed",
      message: "ticket expired",
    });
    const host = createHost();

    handleConnected(host as never);
    await flushMicrotasks();

    expect(connectGatewayMock).not.toHaveBeenCalled();
  });
});
