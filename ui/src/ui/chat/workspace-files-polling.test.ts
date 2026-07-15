import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { loadNodesMock, loadWorkspaceFilesMock } = vi.hoisted(() => ({
  loadNodesMock: vi.fn(),
  loadWorkspaceFilesMock: vi.fn(),
}));

vi.mock("../controllers/nodes.ts", () => ({
  loadNodes: loadNodesMock,
}));

vi.mock("../controllers/workspace-files.ts", () => ({
  loadWorkspaceFiles: loadWorkspaceFilesMock,
}));

import {
  NODES_POLL_INTERVAL_MS,
  startNodesPolling,
  startWorkspaceFilesPolling,
  stopNodesPolling,
  stopWorkspaceFilesPolling,
  WORKSPACE_FILES_POLL_INTERVAL_MS,
} from "../app-polling.ts";

function createHost() {
  return {
    nodesPollInterval: null,
    logsPollInterval: null,
    debugPollInterval: null,
    workspaceFilesPollInterval: null,
    tab: "chat",
    sessionKey: "agent:demo-main:main",
    workspaceFilesLoading: false,
  };
}

describe("workspace files polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("window", {
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
    });
    loadNodesMock.mockReset();
    loadWorkspaceFilesMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("does not refresh workspace files on a timer", () => {
    const host = createHost();

    startWorkspaceFilesPolling(host);
    vi.advanceTimersByTime(5000);

    expect(loadWorkspaceFilesMock).not.toHaveBeenCalled();
    expect(host.workspaceFilesPollInterval).toBeNull();
    stopWorkspaceFilesPolling(host);
  });

  it("keeps stop idempotent when workspace file updates are push-driven", () => {
    const host = createHost();

    startWorkspaceFilesPolling(host);
    stopWorkspaceFilesPolling(host);

    expect(loadWorkspaceFilesMock).not.toHaveBeenCalled();
    expect(host.workspaceFilesPollInterval).toBeNull();
  });

  it("skips automatic workspace file refreshes outside the chat tab", () => {
    const host = createHost();
    host.tab = "settings";

    startWorkspaceFilesPolling(host);
    vi.advanceTimersByTime(WORKSPACE_FILES_POLL_INTERVAL_MS);

    expect(loadWorkspaceFilesMock).not.toHaveBeenCalled();
    expect(host.workspaceFilesPollInterval).toBeNull();
    stopWorkspaceFilesPolling(host);
  });

  it("skips automatic workspace file refreshes while a load is already running", () => {
    const host = createHost();
    host.workspaceFilesLoading = true;

    startWorkspaceFilesPolling(host);
    vi.advanceTimersByTime(WORKSPACE_FILES_POLL_INTERVAL_MS);

    expect(loadWorkspaceFilesMock).not.toHaveBeenCalled();
    expect(host.workspaceFilesPollInterval).toBeNull();
    stopWorkspaceFilesPolling(host);
  });
});

describe("nodes polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("window", {
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
    });
    loadNodesMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("does not refresh nodes at the old five-second interval", () => {
    const host = createHost();

    startNodesPolling(host);
    vi.advanceTimersByTime(5000);

    expect(loadNodesMock).not.toHaveBeenCalled();
    stopNodesPolling(host);
  });

  it("refreshes nodes at the lower-frequency interval", () => {
    const host = createHost();

    startNodesPolling(host);
    vi.advanceTimersByTime(NODES_POLL_INTERVAL_MS);

    expect(loadNodesMock).toHaveBeenCalledWith(host, { quiet: true });
    stopNodesPolling(host);
    expect(host.nodesPollInterval).toBeNull();
  });
});
