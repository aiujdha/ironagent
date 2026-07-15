import type { IronAgentConfig } from "../../config/types.js";

export type DirectoryConfigParams = {
  cfg: IronAgentConfig;
  accountId?: string | null;
  query?: string | null;
  limit?: number | null;
};
