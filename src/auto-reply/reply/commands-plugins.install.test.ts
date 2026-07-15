import fs from "node:fs/promises";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { withTempHome } from "../../config/home-env.test-harness.js";
import { handleCommands } from "./commands-core.js";
import { createCommandWorkspaceHarness } from "./commands-filesystem.test-support.js";
import { buildCommandTestParams } from "./commands.test-harness.js";

const installPluginFromPathMock = vi.fn();
const installPluginFromIronHubMock = vi.fn();
const persistPluginInstallMock = vi.fn();

vi.mock("../../plugins/install.js", async () => {
  const actual = await vi.importActual<typeof import("../../plugins/install.js")>(
    "../../plugins/install.js",
  );
  return {
    ...actual,
    installPluginFromPath: installPluginFromPathMock,
  };
});

vi.mock("../../plugins/ironhub.js", async () => {
  const actual = await vi.importActual<typeof import("../../plugins/ironhub.js")>(
    "../../plugins/ironhub.js",
  );
  return {
    ...actual,
    installPluginFromIronHub: installPluginFromIronHubMock,
  };
});

vi.mock("../../cli/plugins-install-persist.js", () => ({
  persistPluginInstall: persistPluginInstallMock,
}));

const workspaceHarness = createCommandWorkspaceHarness("ironagent-command-plugins-install-");

describe("handleCommands /plugins install", () => {
  afterEach(async () => {
    installPluginFromPathMock.mockReset();
    installPluginFromIronHubMock.mockReset();
    persistPluginInstallMock.mockReset();
    await workspaceHarness.cleanupWorkspaces();
  });

  it("installs a plugin from a local path", async () => {
    installPluginFromPathMock.mockResolvedValue({
      ok: true,
      pluginId: "path-install-plugin",
      targetDir: "/tmp/path-install-plugin",
      version: "0.0.1",
      extensions: ["index.js"],
    });
    persistPluginInstallMock.mockResolvedValue({});

    await withTempHome("ironagent-command-plugins-home-", async () => {
      const workspaceDir = await workspaceHarness.createWorkspace();
      const pluginDir = path.join(workspaceDir, "fixtures", "path-install-plugin");
      await fs.mkdir(pluginDir, { recursive: true });

      const params = buildCommandTestParams(
        `/plugins install ${pluginDir}`,
        {
          commands: {
            text: true,
            plugins: true,
          },
        },
        undefined,
        { workspaceDir },
      );
      params.command.senderIsOwner = true;

      const result = await handleCommands(params);
      expect(result.reply?.text).toContain('Installed plugin "path-install-plugin"');
      expect(installPluginFromPathMock).toHaveBeenCalledWith(
        expect.objectContaining({
          path: pluginDir,
        }),
      );
      expect(persistPluginInstallMock).toHaveBeenCalledWith(
        expect.objectContaining({
          pluginId: "path-install-plugin",
          install: expect.objectContaining({
            source: "path",
            sourcePath: pluginDir,
            installPath: "/tmp/path-install-plugin",
            version: "0.0.1",
          }),
        }),
      );
    });
  });

  it("installs from an explicit ironhub: spec", async () => {
    installPluginFromIronHubMock.mockResolvedValue({
      ok: true,
      pluginId: "ironhub-demo",
      targetDir: "/tmp/ironhub-demo",
      version: "1.2.3",
      extensions: ["index.js"],
      packageName: "@ironagent/ironhub-demo",
      ironhub: {
        source: "ironhub",
        ironhubUrl: "https://ironhub.ai",
        ironhubPackage: "@ironagent/ironhub-demo",
        ironhubFamily: "code-plugin",
        ironhubChannel: "official",
        version: "1.2.3",
        integrity: "sha512-demo",
        resolvedAt: "2026-03-22T12:00:00.000Z",
      },
    });
    persistPluginInstallMock.mockResolvedValue({});

    await withTempHome("ironagent-command-plugins-home-", async () => {
      const workspaceDir = await workspaceHarness.createWorkspace();
      const params = buildCommandTestParams(
        "/plugins install ironhub:@ironagent/ironhub-demo@1.2.3",
        {
          commands: {
            text: true,
            plugins: true,
          },
        },
        undefined,
        { workspaceDir },
      );
      params.command.senderIsOwner = true;

      const result = await handleCommands(params);
      expect(result.reply?.text).toContain('Installed plugin "ironhub-demo"');
      expect(installPluginFromIronHubMock).toHaveBeenCalledWith(
        expect.objectContaining({
          spec: "ironhub:@ironagent/ironhub-demo@1.2.3",
        }),
      );
      expect(persistPluginInstallMock).toHaveBeenCalledWith(
        expect.objectContaining({
          pluginId: "ironhub-demo",
          install: expect.objectContaining({
            source: "ironhub",
            spec: "ironhub:@ironagent/ironhub-demo@1.2.3",
            installPath: "/tmp/ironhub-demo",
            version: "1.2.3",
            integrity: "sha512-demo",
            ironhubPackage: "@ironagent/ironhub-demo",
            ironhubChannel: "official",
          }),
        }),
      );
    });
  });

  it("treats /plugin add as an install alias", async () => {
    installPluginFromIronHubMock.mockResolvedValue({
      ok: true,
      pluginId: "alias-demo",
      targetDir: "/tmp/alias-demo",
      version: "1.0.0",
      extensions: ["index.js"],
      packageName: "@ironagent/alias-demo",
      ironhub: {
        source: "ironhub",
        ironhubUrl: "https://ironhub.ai",
        ironhubPackage: "@ironagent/alias-demo",
        ironhubFamily: "code-plugin",
        ironhubChannel: "official",
        version: "1.0.0",
        integrity: "sha512-alias",
        resolvedAt: "2026-03-23T12:00:00.000Z",
      },
    });
    persistPluginInstallMock.mockResolvedValue({});

    await withTempHome("ironagent-command-plugins-home-", async () => {
      const workspaceDir = await workspaceHarness.createWorkspace();
      const params = buildCommandTestParams(
        "/plugin add ironhub:@ironagent/alias-demo@1.0.0",
        {
          commands: {
            text: true,
            plugins: true,
          },
        },
        undefined,
        { workspaceDir },
      );
      params.command.senderIsOwner = true;

      const result = await handleCommands(params);
      expect(result.reply?.text).toContain('Installed plugin "alias-demo"');
      expect(installPluginFromIronHubMock).toHaveBeenCalledWith(
        expect.objectContaining({
          spec: "ironhub:@ironagent/alias-demo@1.0.0",
        }),
      );
    });
  });
});
