import type { IronAgentConfig } from "../config/config.js";
import { applyPluginAutoEnable } from "../config/plugin-auto-enable.js";
import { resolveRuntimePluginRegistry } from "./loader.js";
import { getMemoryRuntime } from "./memory-state.js";

function ensureMemoryRuntime(cfg?: IronAgentConfig) {
  const current = getMemoryRuntime();
  if (current || !cfg) {
    return current;
  }
  const resolvedConfig = applyPluginAutoEnable({ config: cfg, env: process.env }).config;
  resolveRuntimePluginRegistry({ config: resolvedConfig });
  return getMemoryRuntime();
}

export async function getActiveMemorySearchManager(params: {
  cfg: IronAgentConfig;
  agentId: string;
  purpose?: "default" | "status";
}) {
  const runtime = ensureMemoryRuntime(params.cfg);
  if (!runtime) {
    return { manager: null, error: "memory plugin unavailable" };
  }
  return await runtime.getMemorySearchManager(params);
}

export function resolveActiveMemoryBackendConfig(params: { cfg: IronAgentConfig; agentId: string }) {
  return ensureMemoryRuntime(params.cfg)?.resolveMemoryBackendConfig(params) ?? null;
}

export async function closeActiveMemorySearchManagers(cfg?: IronAgentConfig): Promise<void> {
  void cfg;
  const runtime = getMemoryRuntime();
  await runtime?.closeAllMemorySearchManagers?.();
}
