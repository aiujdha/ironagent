import { getSafeSessionStorage } from "../../local-storage.ts";
import { resolveCurrentAssistantSingleEntryRoute } from "../assistant-route.ts";
import {
  buildWorkspaceFilesApiUrl,
  resolveControlUiApiBaseUrl,
  shouldUseWorkspaceFilesBrowserAdminToken,
  storeWorkspaceFilesAssistantId,
  storeWorkspaceFilesAdminToken,
} from "./workspace-files.ts";

const CONTROL_UI_LAUNCH_TICKET_SESSION_KEY = "ironagent.control.launchTicket.v1";
let launchTicketResolutionInFlight: Promise<ControlUiLaunchResolution> | null = null;

type ControlUiLaunchTicketResolveApiResponse = {
  assistant_id?: string | null;
  gateway_url: string;
  gateway_token: string;
  admin_token?: string | null;
  session_key?: string | null;
  expires_at?: string | null;
};

export type ControlUiLaunchResolution =
  | { status: "none" }
  | {
      status: "resolved";
      gatewayUrl: string;
      gatewayToken: string;
      sessionKey: string;
      assistantId?: string | null;
    }
  | {
      status: "failed";
      code: "control_ui_launch_failed";
      message: string;
    };

export function stashControlUiLaunchTicket(ticket: string) {
  const session = getSafeSessionStorage();
  const normalized = ticket.trim();
  if (!normalized) {
    session?.removeItem(CONTROL_UI_LAUNCH_TICKET_SESSION_KEY);
    return;
  }
  session?.setItem(CONTROL_UI_LAUNCH_TICKET_SESSION_KEY, normalized);
}

function readControlUiLaunchTicket(): string {
  return getSafeSessionStorage()?.getItem(CONTROL_UI_LAUNCH_TICKET_SESSION_KEY)?.trim() ?? "";
}

function clearControlUiLaunchTicket() {
  getSafeSessionStorage()?.removeItem(CONTROL_UI_LAUNCH_TICKET_SESSION_KEY);
}

function launchFailure(message: string): ControlUiLaunchResolution {
  return {
    status: "failed",
    code: "control_ui_launch_failed",
    message,
  };
}

export async function resolveControlUiLaunchTicket(): Promise<ControlUiLaunchResolution> {
  if (launchTicketResolutionInFlight) {
    return launchTicketResolutionInFlight;
  }
  launchTicketResolutionInFlight = (async () => {
    const ticket = readControlUiLaunchTicket();
    if (!ticket) {
      return { status: "none" } satisfies ControlUiLaunchResolution;
    }

    const apiBaseUrl = resolveControlUiApiBaseUrl();
    const singleEntryRoute = resolveCurrentAssistantSingleEntryRoute();

    let response: Response;
    try {
      response = await fetch(
        buildWorkspaceFilesApiUrl(apiBaseUrl, "/api/v1/control-ui/launch-tickets/resolve"),
        {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ ticket }),
        },
      );
    } catch {
      return launchFailure("安全入口解析失败，请检查安全服务连接。");
    }

    if (!response.ok) {
      if (response.status === 403) {
        storeWorkspaceFilesAssistantId(null);
        clearControlUiLaunchTicket();
        return launchFailure("安全入口来源校验失败，请从后台重新打开聊天页。");
      }
      if (response.status === 404 || response.status === 410) {
        storeWorkspaceFilesAssistantId(null);
        clearControlUiLaunchTicket();
        return launchFailure("安全入口已失效，请从后台重新打开聊天页。");
      }
      return launchFailure("安全入口解析失败，请稍后重试。");
    }

    const payload = (await response.json()) as ControlUiLaunchTicketResolveApiResponse;
    const gatewayUrl = singleEntryRoute
      ? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}${
          singleEntryRoute.wsBasePath
        }`
      : (payload.gateway_url?.trim() ?? "");
    const gatewayToken = singleEntryRoute ? "" : (payload.gateway_token?.trim() ?? "");
    const sessionKey = payload.session_key?.trim() || "main";
    if (!gatewayUrl || (!gatewayToken && !singleEntryRoute)) {
      storeWorkspaceFilesAssistantId(null);
      clearControlUiLaunchTicket();
      return launchFailure("安全入口返回的连接信息不完整，请从后台重新打开聊天页。");
    }
    if (payload.admin_token?.trim() && shouldUseWorkspaceFilesBrowserAdminToken(apiBaseUrl)) {
      storeWorkspaceFilesAdminToken(payload.admin_token);
    }
    storeWorkspaceFilesAssistantId(payload.assistant_id ?? null);
    clearControlUiLaunchTicket();
    return {
      status: "resolved",
      gatewayUrl,
      gatewayToken,
      sessionKey,
      assistantId: payload.assistant_id?.trim() || null,
    } satisfies ControlUiLaunchResolution;
  })();
  try {
    return await launchTicketResolutionInFlight;
  } finally {
    launchTicketResolutionInFlight = null;
  }
}
