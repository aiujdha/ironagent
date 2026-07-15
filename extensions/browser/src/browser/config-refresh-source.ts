import { createConfigIO, getRuntimeConfigSnapshot, type IronAgentConfig } from "../config/config.js";

export function loadBrowserConfigForRuntimeRefresh(): IronAgentConfig {
  return getRuntimeConfigSnapshot() ?? createConfigIO().loadConfig();
}
