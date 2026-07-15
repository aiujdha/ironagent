import { beforeEach, describe, expect, it } from "vitest";
import { installedPluginRoot } from "../../test/helpers/bundled-plugin-paths.js";
import type { IronAgentConfig } from "../config/config.js";
import {
  applyExclusiveSlotSelection,
  buildPluginStatusReport,
  clearPluginManifestRegistryCache,
  enablePluginInConfig,
  installHooksFromNpmSpec,
  installPluginFromIronHub,
  installPluginFromMarketplace,
  installPluginFromNpmSpec,
  loadConfig,
  readConfigFileSnapshot,
  parseIronHubPluginSpec,
  recordHookInstall,
  recordPluginInstall,
  resetPluginsCliTestState,
  runPluginsCommand,
  runtimeErrors,
  runtimeLogs,
  writeConfigFile,
} from "./plugins-cli-test-helpers.js";

const CLI_STATE_ROOT = "/tmp/ironagent-state";

function cliInstallPath(pluginId: string): string {
  return installedPluginRoot(CLI_STATE_ROOT, pluginId);
}

function createEnabledPluginConfig(pluginId: string): IronAgentConfig {
  return {
    plugins: {
      entries: {
        [pluginId]: {
          enabled: true,
        },
      },
    },
  } as IronAgentConfig;
}

function createIronHubInstalledConfig(params: {
  pluginId: string;
  install: Record<string, unknown>;
}): IronAgentConfig {
  const enabledCfg = createEnabledPluginConfig(params.pluginId);
  return {
    ...enabledCfg,
    plugins: {
      ...enabledCfg.plugins,
      installs: {
        [params.pluginId]: params.install,
      },
    },
  } as IronAgentConfig;
}

function createIronHubInstallResult(params: {
  pluginId: string;
  packageName: string;
  version: string;
  channel: string;
}): Awaited<ReturnType<typeof installPluginFromIronHub>> {
  return {
    ok: true,
    pluginId: params.pluginId,
    targetDir: cliInstallPath(params.pluginId),
    version: params.version,
    packageName: params.packageName,
    ironhub: {
      source: "ironhub",
      ironhubUrl: "https://ironhub.ai",
      ironhubPackage: params.packageName,
      ironhubFamily: "code-plugin",
      ironhubChannel: params.channel,
      version: params.version,
      integrity: "sha256-abc",
      resolvedAt: "2026-03-22T00:00:00.000Z",
    },
  };
}

describe("plugins cli install", () => {
  beforeEach(() => {
    resetPluginsCliTestState();
  });

  it("exits when --marketplace is combined with --link", async () => {
    await expect(
      runPluginsCommand(["plugins", "install", "alpha", "--marketplace", "local/repo", "--link"]),
    ).rejects.toThrow("__exit__:1");

    expect(runtimeErrors.at(-1)).toContain("`--link` is not supported with `--marketplace`.");
    expect(installPluginFromMarketplace).not.toHaveBeenCalled();
  });

  it("exits when marketplace install fails", async () => {
    await expect(
      runPluginsCommand(["plugins", "install", "alpha", "--marketplace", "local/repo"]),
    ).rejects.toThrow("__exit__:1");

    expect(installPluginFromMarketplace).toHaveBeenCalledWith(
      expect.objectContaining({
        marketplace: "local/repo",
        plugin: "alpha",
      }),
    );
    expect(writeConfigFile).not.toHaveBeenCalled();
  });

  it("fails closed for unrelated invalid config before installer side effects", async () => {
    const invalidConfigErr = new Error("config invalid");
    (invalidConfigErr as { code?: string }).code = "INVALID_CONFIG";
    loadConfig.mockImplementation(() => {
      throw invalidConfigErr;
    });
    readConfigFileSnapshot.mockResolvedValue({
      path: "/tmp/ironagent-config.json5",
      exists: true,
      raw: '{ "models": { "default": 123 } }',
      parsed: { models: { default: 123 } },
      resolved: { models: { default: 123 } },
      valid: false,
      config: { models: { default: 123 } },
      hash: "mock",
      issues: [{ path: "models.default", message: "invalid model ref" }],
      warnings: [],
      legacyIssues: [],
    });

    await expect(runPluginsCommand(["plugins", "install", "alpha"])).rejects.toThrow("__exit__:1");

    expect(runtimeErrors.at(-1)).toContain(
      "Config invalid; run `ironagent doctor --fix` before installing plugins.",
    );
    expect(installPluginFromMarketplace).not.toHaveBeenCalled();
    expect(installPluginFromNpmSpec).not.toHaveBeenCalled();
    expect(writeConfigFile).not.toHaveBeenCalled();
  });

  it("installs marketplace plugins and persists config", async () => {
    const cfg = {
      plugins: {
        entries: {},
      },
    } as IronAgentConfig;
    const enabledCfg = {
      plugins: {
        entries: {
          alpha: {
            enabled: true,
          },
        },
      },
    } as IronAgentConfig;
    const installedCfg = {
      ...enabledCfg,
      plugins: {
        ...enabledCfg.plugins,
        installs: {
          alpha: {
            source: "marketplace",
            installPath: cliInstallPath("alpha"),
          },
        },
      },
    } as IronAgentConfig;

    loadConfig.mockReturnValue(cfg);
    installPluginFromMarketplace.mockResolvedValue({
      ok: true,
      pluginId: "alpha",
      targetDir: cliInstallPath("alpha"),
      version: "1.2.3",
      marketplaceName: "Claude",
      marketplaceSource: "local/repo",
      marketplacePlugin: "alpha",
    });
    enablePluginInConfig.mockReturnValue({ config: enabledCfg });
    recordPluginInstall.mockReturnValue(installedCfg);
    buildPluginStatusReport.mockReturnValue({
      plugins: [{ id: "alpha", kind: "provider" }],
      diagnostics: [],
    });
    applyExclusiveSlotSelection.mockReturnValue({
      config: installedCfg,
      warnings: ["slot adjusted"],
    });

    await runPluginsCommand(["plugins", "install", "alpha", "--marketplace", "local/repo"]);

    expect(clearPluginManifestRegistryCache).toHaveBeenCalledTimes(1);
    expect(writeConfigFile).toHaveBeenCalledWith(installedCfg);
    expect(runtimeLogs.some((line) => line.includes("slot adjusted"))).toBe(true);
    expect(runtimeLogs.some((line) => line.includes("Installed plugin: alpha"))).toBe(true);
  });

  it("installs IronHub plugins and persists source metadata", async () => {
    const cfg = {
      plugins: {
        entries: {},
      },
    } as IronAgentConfig;
    const enabledCfg = createEnabledPluginConfig("demo");
    const installedCfg = createIronHubInstalledConfig({
      pluginId: "demo",
      install: {
        source: "ironhub",
        spec: "ironhub:demo@1.2.3",
        installPath: cliInstallPath("demo"),
        ironhubPackage: "demo",
        ironhubFamily: "code-plugin",
        ironhubChannel: "official",
      },
    });

    loadConfig.mockReturnValue(cfg);
    parseIronHubPluginSpec.mockReturnValue({ name: "demo" });
    installPluginFromIronHub.mockResolvedValue(
      createIronHubInstallResult({
        pluginId: "demo",
        packageName: "demo",
        version: "1.2.3",
        channel: "official",
      }),
    );
    enablePluginInConfig.mockReturnValue({ config: enabledCfg });
    recordPluginInstall.mockReturnValue(installedCfg);
    applyExclusiveSlotSelection.mockReturnValue({
      config: installedCfg,
      warnings: [],
    });

    await runPluginsCommand(["plugins", "install", "ironhub:demo"]);

    expect(installPluginFromIronHub).toHaveBeenCalledWith(
      expect.objectContaining({
        spec: "ironhub:demo",
      }),
    );
    expect(recordPluginInstall).toHaveBeenCalledWith(
      enabledCfg,
      expect.objectContaining({
        pluginId: "demo",
        source: "ironhub",
        spec: "ironhub:demo@1.2.3",
        ironhubPackage: "demo",
        ironhubFamily: "code-plugin",
        ironhubChannel: "official",
      }),
    );
    expect(writeConfigFile).toHaveBeenCalledWith(installedCfg);
    expect(runtimeLogs.some((line) => line.includes("Installed plugin: demo"))).toBe(true);
    expect(installPluginFromNpmSpec).not.toHaveBeenCalled();
  });

  it("prefers IronHub before npm for bare plugin specs", async () => {
    const cfg = {
      plugins: {
        entries: {},
      },
    } as IronAgentConfig;
    const enabledCfg = createEnabledPluginConfig("demo");
    const installedCfg = createIronHubInstalledConfig({
      pluginId: "demo",
      install: {
        source: "ironhub",
        spec: "ironhub:demo@1.2.3",
        installPath: cliInstallPath("demo"),
        ironhubPackage: "demo",
      },
    });

    loadConfig.mockReturnValue(cfg);
    installPluginFromIronHub.mockResolvedValue(
      createIronHubInstallResult({
        pluginId: "demo",
        packageName: "demo",
        version: "1.2.3",
        channel: "community",
      }),
    );
    enablePluginInConfig.mockReturnValue({ config: enabledCfg });
    recordPluginInstall.mockReturnValue(installedCfg);
    applyExclusiveSlotSelection.mockReturnValue({
      config: installedCfg,
      warnings: [],
    });

    await runPluginsCommand(["plugins", "install", "demo"]);

    expect(installPluginFromIronHub).toHaveBeenCalledWith(
      expect.objectContaining({
        spec: "ironhub:demo",
      }),
    );
    expect(installPluginFromNpmSpec).not.toHaveBeenCalled();
    expect(writeConfigFile).toHaveBeenCalledWith(installedCfg);
  });

  it("falls back to npm when IronHub does not have the package", async () => {
    const cfg = {
      plugins: {
        entries: {},
      },
    } as IronAgentConfig;
    const enabledCfg = {
      plugins: {
        entries: {
          demo: {
            enabled: true,
          },
        },
      },
    } as IronAgentConfig;

    loadConfig.mockReturnValue(cfg);
    installPluginFromIronHub.mockResolvedValue({
      ok: false,
      error: "IronHub /api/v1/packages/demo failed (404): Package not found",
      code: "package_not_found",
    });
    installPluginFromNpmSpec.mockResolvedValue({
      ok: true,
      pluginId: "demo",
      targetDir: cliInstallPath("demo"),
      version: "1.2.3",
      npmResolution: {
        packageName: "demo",
        resolvedVersion: "1.2.3",
        tarballUrl: "https://registry.npmjs.org/demo/-/demo-1.2.3.tgz",
      },
    });
    enablePluginInConfig.mockReturnValue({ config: enabledCfg });
    recordPluginInstall.mockReturnValue(enabledCfg);
    applyExclusiveSlotSelection.mockReturnValue({
      config: enabledCfg,
      warnings: [],
    });

    await runPluginsCommand(["plugins", "install", "demo"]);

    expect(installPluginFromIronHub).toHaveBeenCalledWith(
      expect.objectContaining({
        spec: "ironhub:demo",
      }),
    );
    expect(installPluginFromNpmSpec).toHaveBeenCalledWith(
      expect.objectContaining({
        spec: "demo",
      }),
    );
  });

  it("passes dangerous force unsafe install to marketplace installs", async () => {
    await expect(
      runPluginsCommand([
        "plugins",
        "install",
        "alpha",
        "--marketplace",
        "local/repo",
        "--dangerously-force-unsafe-install",
      ]),
    ).rejects.toThrow("__exit__:1");

    expect(installPluginFromMarketplace).toHaveBeenCalledWith(
      expect.objectContaining({
        marketplace: "local/repo",
        plugin: "alpha",
        dangerouslyForceUnsafeInstall: true,
      }),
    );
  });

  it("passes dangerous force unsafe install to npm installs", async () => {
    const cfg = {
      plugins: {
        entries: {},
      },
    } as IronAgentConfig;
    const enabledCfg = createEnabledPluginConfig("demo");

    loadConfig.mockReturnValue(cfg);
    installPluginFromIronHub.mockResolvedValue({
      ok: false,
      error: "IronHub /api/v1/packages/demo failed (404): Package not found",
      code: "package_not_found",
    });
    installPluginFromNpmSpec.mockResolvedValue({
      ok: true,
      pluginId: "demo",
      targetDir: cliInstallPath("demo"),
      version: "1.2.3",
      npmResolution: {
        packageName: "demo",
        resolvedVersion: "1.2.3",
        tarballUrl: "https://registry.npmjs.org/demo/-/demo-1.2.3.tgz",
      },
    });
    enablePluginInConfig.mockReturnValue({ config: enabledCfg });
    recordPluginInstall.mockReturnValue(enabledCfg);
    applyExclusiveSlotSelection.mockReturnValue({
      config: enabledCfg,
      warnings: [],
    });

    await runPluginsCommand(["plugins", "install", "demo", "--dangerously-force-unsafe-install"]);

    expect(installPluginFromNpmSpec).toHaveBeenCalledWith(
      expect.objectContaining({
        spec: "demo",
        dangerouslyForceUnsafeInstall: true,
      }),
    );
  });

  it("does not fall back to npm when IronHub rejects a real package", async () => {
    installPluginFromIronHub.mockResolvedValue({
      ok: false,
      error: 'Use "ironagent skills install demo" instead.',
      code: "skill_package",
    });

    await expect(runPluginsCommand(["plugins", "install", "demo"])).rejects.toThrow("__exit__:1");

    expect(installPluginFromNpmSpec).not.toHaveBeenCalled();
    expect(runtimeErrors.at(-1)).toContain('Use "ironagent skills install demo" instead.');
  });

  it("falls back to installing hook packs from npm specs", async () => {
    const cfg = {} as IronAgentConfig;
    const installedCfg = {
      hooks: {
        internal: {
          installs: {
            "demo-hooks": {
              source: "npm",
              spec: "@acme/demo-hooks@1.2.3",
            },
          },
        },
      },
    } as IronAgentConfig;

    loadConfig.mockReturnValue(cfg);
    installPluginFromIronHub.mockResolvedValue({
      ok: false,
      error: "IronHub /api/v1/packages/@acme/demo-hooks failed (404): Package not found",
      code: "package_not_found",
    });
    installPluginFromNpmSpec.mockResolvedValue({
      ok: false,
      error: "package.json missing ironagent.plugin.json",
    });
    installHooksFromNpmSpec.mockResolvedValue({
      ok: true,
      hookPackId: "demo-hooks",
      hooks: ["command-audit"],
      targetDir: "/tmp/hooks/demo-hooks",
      version: "1.2.3",
      npmResolution: {
        name: "@acme/demo-hooks",
        spec: "@acme/demo-hooks@1.2.3",
        integrity: "sha256-demo",
      },
    });
    recordHookInstall.mockReturnValue(installedCfg);

    await runPluginsCommand(["plugins", "install", "@acme/demo-hooks"]);

    expect(installHooksFromNpmSpec).toHaveBeenCalledWith(
      expect.objectContaining({
        spec: "@acme/demo-hooks",
      }),
    );
    expect(recordHookInstall).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        hookId: "demo-hooks",
        hooks: ["command-audit"],
      }),
    );
    expect(writeConfigFile).toHaveBeenCalledWith(installedCfg);
    expect(runtimeLogs.some((line) => line.includes("Installed hook pack: demo-hooks"))).toBe(true);
  });
});
