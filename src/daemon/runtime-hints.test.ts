import { describe, expect, it } from "vitest";
import { buildPlatformRuntimeLogHints, buildPlatformServiceStartHints } from "./runtime-hints.js";

describe("buildPlatformRuntimeLogHints", () => {
  it("renders launchd log hints on darwin", () => {
    expect(
      buildPlatformRuntimeLogHints({
        platform: "darwin",
        env: {
          IRONAGENT_STATE_DIR: "/tmp/ironagent-state",
          IRONAGENT_LOG_PREFIX: "gateway",
        },
        systemdServiceName: "ironagent-gateway",
        windowsTaskName: "IronAgent Gateway",
      }),
    ).toEqual([
      "Launchd stdout (if installed): /tmp/ironagent-state/logs/gateway.log",
      "Launchd stderr (if installed): /tmp/ironagent-state/logs/gateway.err.log",
    ]);
  });

  it("renders systemd and windows hints by platform", () => {
    expect(
      buildPlatformRuntimeLogHints({
        platform: "linux",
        systemdServiceName: "ironagent-gateway",
        windowsTaskName: "IronAgent Gateway",
      }),
    ).toEqual(["Logs: journalctl --user -u ironagent-gateway.service -n 200 --no-pager"]);
    expect(
      buildPlatformRuntimeLogHints({
        platform: "win32",
        systemdServiceName: "ironagent-gateway",
        windowsTaskName: "IronAgent Gateway",
      }),
    ).toEqual(['Logs: schtasks /Query /TN "IronAgent Gateway" /V /FO LIST']);
  });
});

describe("buildPlatformServiceStartHints", () => {
  it("builds platform-specific service start hints", () => {
    expect(
      buildPlatformServiceStartHints({
        platform: "darwin",
        installCommand: "ironagent gateway install",
        startCommand: "ironagent gateway",
        launchAgentPlistPath: "~/Library/LaunchAgents/com.ironagent.gateway.plist",
        systemdServiceName: "ironagent-gateway",
        windowsTaskName: "IronAgent Gateway",
      }),
    ).toEqual([
      "ironagent gateway install",
      "ironagent gateway",
      "launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ironagent.gateway.plist",
    ]);
    expect(
      buildPlatformServiceStartHints({
        platform: "linux",
        installCommand: "ironagent gateway install",
        startCommand: "ironagent gateway",
        launchAgentPlistPath: "~/Library/LaunchAgents/com.ironagent.gateway.plist",
        systemdServiceName: "ironagent-gateway",
        windowsTaskName: "IronAgent Gateway",
      }),
    ).toEqual([
      "ironagent gateway install",
      "ironagent gateway",
      "systemctl --user start ironagent-gateway.service",
    ]);
  });
});
