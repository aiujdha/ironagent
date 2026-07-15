import type { IronAgentConfig } from "ironagent/plugin-sdk/browser-support";
import {
  normalizePluginsConfig,
  resolveEffectiveEnableState,
} from "ironagent/plugin-sdk/browser-support";

export function isDefaultBrowserPluginEnabled(cfg: IronAgentConfig): boolean {
  return resolveEffectiveEnableState({
    id: "browser",
    origin: "bundled",
    config: normalizePluginsConfig(cfg.plugins),
    rootConfig: cfg,
    enabledByDefault: true,
  }).enabled;
}
