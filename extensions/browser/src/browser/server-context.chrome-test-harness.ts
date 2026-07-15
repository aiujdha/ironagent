import { vi } from "vitest";
import { installChromeUserDataDirHooks } from "./chrome-user-data-dir.test-harness.js";

const chromeUserDataDir = { dir: "/tmp/ironagent" };
installChromeUserDataDirHooks(chromeUserDataDir);

vi.mock("./chrome.js", () => ({
  isChromeCdpReady: vi.fn(async () => true),
  isChromeReachable: vi.fn(async () => true),
  launchIronAgentChrome: vi.fn(async () => {
    throw new Error("unexpected launch");
  }),
  resolveIronAgentUserDataDir: vi.fn(() => chromeUserDataDir.dir),
  stopIronAgentChrome: vi.fn(async () => {}),
}));
