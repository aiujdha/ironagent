import { Command } from "commander";
import { vi } from "vitest";
import type { IronAgentConfig } from "../config/config.js";
import { createCliRuntimeCapture } from "./test-runtime-capture.js";

export const loadConfig = vi.fn<() => IronAgentConfig>(() => ({}) as IronAgentConfig);
export const readConfigFileSnapshot = vi.fn();
export const writeConfigFile = vi.fn<(config: IronAgentConfig) => Promise<void>>(
  async () => undefined,
);
export const replaceConfigFile = vi.fn(
  async (params: { nextConfig: IronAgentConfig }) => await writeConfigFile(params.nextConfig),
);
export const resolveStateDir = vi.fn(() => "/tmp/ironagent-state");
export const installPluginFromMarketplace = vi.fn();
export const listMarketplacePlugins = vi.fn();
export const resolveMarketplaceInstallShortcut = vi.fn();
export const enablePluginInConfig = vi.fn();
export const recordPluginInstall = vi.fn();
export const clearPluginManifestRegistryCache = vi.fn();
export const buildPluginStatusReport = vi.fn();
export const applyExclusiveSlotSelection = vi.fn();
export const uninstallPlugin = vi.fn();
export const updateNpmInstalledPlugins = vi.fn();
export const updateNpmInstalledHookPacks = vi.fn();
export const promptYesNo = vi.fn();
export const installPluginFromNpmSpec = vi.fn();
export const installPluginFromPath = vi.fn();
export const installPluginFromIronHub = vi.fn();
export const parseIronHubPluginSpec = vi.fn();
export const installHooksFromNpmSpec = vi.fn();
export const installHooksFromPath = vi.fn();
export const recordHookInstall = vi.fn();

const { defaultRuntime, runtimeLogs, runtimeErrors, resetRuntimeCapture } =
  createCliRuntimeCapture();

export { runtimeErrors, runtimeLogs };

vi.mock("../runtime.js", () => ({
  defaultRuntime,
}));

vi.mock("../config/config.js", () => ({
  loadConfig: () => loadConfig(),
  readConfigFileSnapshot: (...args: unknown[]) => readConfigFileSnapshot(...args),
  writeConfigFile: (config: IronAgentConfig) => writeConfigFile(config),
  replaceConfigFile: (params: { nextConfig: IronAgentConfig }) => replaceConfigFile(params),
}));

vi.mock("../config/paths.js", () => ({
  resolveStateDir: () => resolveStateDir(),
}));

vi.mock("../plugins/marketplace.js", () => ({
  installPluginFromMarketplace: (...args: unknown[]) => installPluginFromMarketplace(...args),
  listMarketplacePlugins: (...args: unknown[]) => listMarketplacePlugins(...args),
  resolveMarketplaceInstallShortcut: (...args: unknown[]) =>
    resolveMarketplaceInstallShortcut(...args),
}));

vi.mock("../plugins/enable.js", () => ({
  enablePluginInConfig: (...args: unknown[]) => enablePluginInConfig(...args),
}));

vi.mock("../plugins/installs.js", () => ({
  recordPluginInstall: (...args: unknown[]) => recordPluginInstall(...args),
}));

vi.mock("../plugins/manifest-registry.js", () => ({
  clearPluginManifestRegistryCache: () => clearPluginManifestRegistryCache(),
}));

vi.mock("../plugins/status.js", () => ({
  buildPluginStatusReport: (...args: unknown[]) => buildPluginStatusReport(...args),
}));

vi.mock("../plugins/slots.js", () => ({
  applyExclusiveSlotSelection: (...args: unknown[]) => applyExclusiveSlotSelection(...args),
}));

vi.mock("../plugins/uninstall.js", () => ({
  uninstallPlugin: (...args: unknown[]) => uninstallPlugin(...args),
  resolveUninstallDirectoryTarget: ({
    installRecord,
  }: {
    installRecord?: { installPath?: string; sourcePath?: string };
  }) => installRecord?.installPath ?? installRecord?.sourcePath ?? null,
}));

vi.mock("../plugins/update.js", () => ({
  updateNpmInstalledPlugins: (...args: unknown[]) => updateNpmInstalledPlugins(...args),
}));

vi.mock("../hooks/update.js", () => ({
  updateNpmInstalledHookPacks: (...args: unknown[]) => updateNpmInstalledHookPacks(...args),
}));

vi.mock("./prompt.js", () => ({
  promptYesNo: (...args: unknown[]) => promptYesNo(...args),
}));

vi.mock("../plugins/install.js", () => ({
  PLUGIN_INSTALL_ERROR_CODE: {
    NPM_PACKAGE_NOT_FOUND: "npm_package_not_found",
  },
  installPluginFromNpmSpec: (...args: unknown[]) => installPluginFromNpmSpec(...args),
  installPluginFromPath: (...args: unknown[]) => installPluginFromPath(...args),
}));

vi.mock("../hooks/install.js", () => ({
  installHooksFromNpmSpec: (...args: unknown[]) => installHooksFromNpmSpec(...args),
  installHooksFromPath: (...args: unknown[]) => installHooksFromPath(...args),
  resolveHookInstallDir: (hookId: string) => `/tmp/hooks/${hookId}`,
}));

vi.mock("../hooks/installs.js", () => ({
  recordHookInstall: (...args: unknown[]) => recordHookInstall(...args),
}));

vi.mock("../plugins/ironhub.js", () => ({
  IRONHUB_INSTALL_ERROR_CODE: {
    PACKAGE_NOT_FOUND: "package_not_found",
    VERSION_NOT_FOUND: "version_not_found",
  },
  installPluginFromIronHub: (...args: unknown[]) => installPluginFromIronHub(...args),
  formatIronHubSpecifier: ({ name, version }: { name: string; version?: string }) =>
    `ironhub:${name}${version ? `@${version}` : ""}`,
}));

vi.mock("../infra/ironhub.js", () => ({
  parseIronHubPluginSpec: (...args: unknown[]) => parseIronHubPluginSpec(...args),
}));

const { registerPluginsCli } = await import("./plugins-cli.js");

export function runPluginsCommand(argv: string[]) {
  const program = new Command();
  program.exitOverride();
  registerPluginsCli(program);
  return program.parseAsync(argv, { from: "user" });
}

export function resetPluginsCliTestState() {
  resetRuntimeCapture();
  loadConfig.mockReset();
  readConfigFileSnapshot.mockReset();
  writeConfigFile.mockReset();
  replaceConfigFile.mockReset();
  resolveStateDir.mockReset();
  installPluginFromMarketplace.mockReset();
  listMarketplacePlugins.mockReset();
  resolveMarketplaceInstallShortcut.mockReset();
  enablePluginInConfig.mockReset();
  recordPluginInstall.mockReset();
  clearPluginManifestRegistryCache.mockReset();
  buildPluginStatusReport.mockReset();
  applyExclusiveSlotSelection.mockReset();
  uninstallPlugin.mockReset();
  updateNpmInstalledPlugins.mockReset();
  updateNpmInstalledHookPacks.mockReset();
  promptYesNo.mockReset();
  installPluginFromNpmSpec.mockReset();
  installPluginFromPath.mockReset();
  installPluginFromIronHub.mockReset();
  parseIronHubPluginSpec.mockReset();
  installHooksFromNpmSpec.mockReset();
  installHooksFromPath.mockReset();
  recordHookInstall.mockReset();

  loadConfig.mockReturnValue({} as IronAgentConfig);
  readConfigFileSnapshot.mockImplementation(async () => {
    const config = loadConfig();
    return {
      path: "/tmp/ironagent-config.json5",
      exists: true,
      raw: "{}",
      parsed: config,
      resolved: config,
      sourceConfig: config,
      runtimeConfig: config,
      valid: true,
      config,
      hash: "mock",
      issues: [],
      warnings: [],
      legacyIssues: [],
    };
  });
  writeConfigFile.mockResolvedValue(undefined);
  replaceConfigFile.mockImplementation(
    async (params: { nextConfig: IronAgentConfig }) => await writeConfigFile(params.nextConfig),
  );
  resolveStateDir.mockReturnValue("/tmp/ironagent-state");
  resolveMarketplaceInstallShortcut.mockResolvedValue(null);
  installPluginFromMarketplace.mockResolvedValue({
    ok: false,
    error: "marketplace install failed",
  });
  enablePluginInConfig.mockImplementation((cfg: IronAgentConfig) => ({ config: cfg }));
  recordPluginInstall.mockImplementation((cfg: IronAgentConfig) => cfg);
  buildPluginStatusReport.mockReturnValue({
    plugins: [],
    diagnostics: [],
  });
  applyExclusiveSlotSelection.mockImplementation(({ config }: { config: IronAgentConfig }) => ({
    config,
    warnings: [],
  }));
  uninstallPlugin.mockResolvedValue({
    ok: true,
    config: {} as IronAgentConfig,
    warnings: [],
    actions: {
      entry: false,
      install: false,
      allowlist: false,
      loadPath: false,
      memorySlot: false,
      directory: false,
    },
  });
  updateNpmInstalledPlugins.mockResolvedValue({
    outcomes: [],
    changed: false,
    config: {} as IronAgentConfig,
  });
  updateNpmInstalledHookPacks.mockResolvedValue({
    outcomes: [],
    changed: false,
    config: {} as IronAgentConfig,
  });
  promptYesNo.mockResolvedValue(true);
  installPluginFromPath.mockResolvedValue({ ok: false, error: "path install disabled in test" });
  installPluginFromNpmSpec.mockResolvedValue({
    ok: false,
    error: "npm install disabled in test",
  });
  installPluginFromIronHub.mockResolvedValue({
    ok: false,
    error: "ironhub install disabled in test",
  });
  parseIronHubPluginSpec.mockReturnValue(null);
  installHooksFromPath.mockResolvedValue({
    ok: false,
    error: "hook path install disabled in test",
  });
  installHooksFromNpmSpec.mockResolvedValue({
    ok: false,
    error: "hook npm install disabled in test",
  });
  recordHookInstall.mockImplementation((cfg: IronAgentConfig) => cfg);
}
