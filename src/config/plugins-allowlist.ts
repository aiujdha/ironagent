import type { IronAgentConfig } from "./config.js";

export function ensurePluginAllowlisted(cfg: IronAgentConfig, pluginId: string): IronAgentConfig {
  const allow = cfg.plugins?.allow;
  if (!Array.isArray(allow) || allow.includes(pluginId)) {
    return cfg;
  }
  return {
    ...cfg,
    plugins: {
      ...cfg.plugins,
      allow: [...allow, pluginId],
    },
  };
}
