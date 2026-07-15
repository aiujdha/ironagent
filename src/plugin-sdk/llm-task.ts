// Narrow plugin-sdk surface for the bundled llm-task plugin.
// Keep this list additive and scoped to the bundled LLM task surface.

export { definePluginEntry } from "./plugin-entry.js";
export { resolvePreferredIronAgentTmpDir } from "../infra/tmp-ironagent-dir.js";
export {
  formatThinkingLevels,
  formatXHighModelHint,
  normalizeThinkLevel,
  supportsXHighThinking,
} from "../auto-reply/thinking.js";
export type { AnyAgentTool, IronAgentPluginApi } from "../plugins/types.js";
