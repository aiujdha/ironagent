import { getRuntimeConfigSnapshot, type IronAgentConfig } from "../../config/config.js";

export function resolveSkillRuntimeConfig(config?: IronAgentConfig): IronAgentConfig | undefined {
  return getRuntimeConfigSnapshot() ?? config;
}
