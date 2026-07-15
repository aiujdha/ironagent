// Narrow plugin-sdk surface for the bundled diffs plugin.
// Keep this list additive and scoped to the bundled diffs surface.

export { definePluginEntry } from "./plugin-entry.js";
export type { IronAgentConfig } from "../config/config.js";
export { resolvePreferredIronAgentTmpDir } from "../infra/tmp-ironagent-dir.js";
export type {
  AnyAgentTool,
  IronAgentPluginApi,
  IronAgentPluginConfigSchema,
  IronAgentPluginToolContext,
  PluginLogger,
} from "../plugins/types.js";
