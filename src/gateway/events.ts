import type { UpdateAvailable } from "../infra/update-startup.js";

export const GATEWAY_EVENT_UPDATE_AVAILABLE = "update.available" as const;
export const GATEWAY_EVENT_WORKSPACE_FILES_CHANGED = "workspace.files.changed" as const;

export type GatewayUpdateAvailableEventPayload = {
  updateAvailable: UpdateAvailable | null;
};

export type GatewayWorkspaceFilesChangedEventPayload = {
  assistantId: string;
  changedPath?: string;
  source: "watch";
  ts: number;
};
