import { resolveActiveTalkProviderConfig } from "../../config/talk.js";
import type { IronAgentConfig } from "../../config/types.js";

export { resolveActiveTalkProviderConfig };

export function getRuntimeConfigSnapshot(): IronAgentConfig | null {
  return null;
}
