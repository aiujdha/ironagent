/**
 * IronAgent Approval Poller
 *
 * Polls the IronAgent Security Service /api/v1/approvals for pending approvals
 * and feeds them into the WebUI's execApprovalQueue so they appear in the
 * approval overlay alongside IronAgent plugin approvals.
 */

import type { ExecApprovalRequest } from "./exec-approval.ts";
import {
  buildWorkspaceFilesApiUrl,
  resolveWorkspaceFilesAdminToken,
  resolveWorkspaceFilesApiBaseUrl,
} from "./workspace-files.ts";

export type IronAgentApprovalItem = {
  approval_id: string;
  tool_name: string;
  assistant_id: string;
  capability_profile: string;
  approval_scope: string;
  reason: string | null;
  target: string | null;
  status: string;
  resolution: string | null;
  requested_at: string;
  expires_at: string;
  resolved_at: string | null;
};

export type IronAgentApprovalPollResult = {
  items: IronAgentApprovalItem[];
};

const POLL_INTERVAL_MS = 2000;
const DEFAULT_IRONAGENT_APPROVAL_TIMEOUT_MS = 15 * 60 * 1000;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let pollingActive = false;
let pollInFlight = false;
let pollListener: ((approvals: ExecApprovalRequest[]) => void) | null = null;

function ironagentApiBaseUrl(): string {
  const base = resolveWorkspaceFilesApiBaseUrl();
  // The workspace files API base is the security service URL
  return base;
}

function buildIronagentAdminResolveUrl(approvalId: string): string {
  return buildWorkspaceFilesApiUrl(
    ironagentApiBaseUrl(),
    `/api/v1/admin/approvals/${encodeURIComponent(approvalId)}/resolve`,
  );
}

let _adminToken: string = "";

export function setIronagentAdminToken(token: string) {
  _adminToken = token.trim();
}

function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  const resolvedToken = _adminToken || resolveWorkspaceFilesAdminToken().trim();
  if (resolvedToken) {
    headers["X-Admin-Token"] = resolvedToken;
  }
  return headers;
}

export async function fetchPendingIronagentApprovals(): Promise<IronAgentApprovalItem[]> {
  const base = ironagentApiBaseUrl();
  if (!base) {
    return [];
  }
  try {
    const url = buildWorkspaceFilesApiUrl(base, "/api/v1/approvals?status=pending");
    const response = await fetch(url, {
      method: "GET",
      headers: getAuthHeaders(),
    });
    if (!response.ok) {
      return [];
    }
    const data = (await response.json()) as IronAgentApprovalItem[];
    return Array.isArray(data) ? data : [];
  } catch {
    return [];
  }
}

export async function resolveIronagentApproval(
  approvalId: string,
  decision: "allow" | "deny",
): Promise<boolean> {
  const url = buildIronagentAdminResolveUrl(approvalId);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        ...getAuthHeaders(),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ resolution: decision }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export function mapIronagentToExecApproval(item: IronAgentApprovalItem): ExecApprovalRequest | null {
  const now = Date.now();
  const expiresAt = item.expires_at
    ? new Date(item.expires_at).getTime()
    : now + DEFAULT_IRONAGENT_APPROVAL_TIMEOUT_MS;
  const requestedAt = item.requested_at ? new Date(item.requested_at).getTime() : now;

  if (!item.approval_id || !item.tool_name) {
    return null;
  }

  // Use IronAgent's approval_id as the unique ID, prefixed to avoid collisions
  const id = `ironagent:${item.approval_id}`;

  return {
    id,
    kind: "plugin",
    request: {
      command: item.tool_name,
      agentId: item.assistant_id || null,
      sessionKey: null,
    },
    pluginTitle: `Approval required: ${item.tool_name}`,
    pluginDescription: item.reason || `Tool ${item.tool_name} requires approval.`,
    pluginSeverity: "warning",
    pluginId: "ironagent-hook-bridge",
    pluginMetadata: {
      source: "ironagent",
      approvalId: item.approval_id,
      approvalScope: item.approval_scope || "admin",
      target: item.target || undefined,
    },
    createdAtMs: requestedAt,
    expiresAtMs: expiresAt,
  };
}

function scheduleNextPoll(delayMs: number) {
  if (!pollingActive) {
    return;
  }
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
  }
  pollTimer = setTimeout(() => {
    void runPollCycle();
  }, delayMs);
}

async function runPollCycle() {
  if (!pollingActive || pollInFlight) {
    return;
  }
  pollInFlight = true;
  try {
    const items = await fetchPendingIronagentApprovals();
    const approvals = items
      .map((item) => mapIronagentToExecApproval(item))
      .filter((item): item is ExecApprovalRequest => item !== null);
    pollListener?.(approvals);
  } catch {
    // Keep the current UI state on transient fetch failures and retry shortly.
  } finally {
    pollInFlight = false;
    scheduleNextPoll(POLL_INTERVAL_MS);
  }
}

/**
 * Start polling IronAgent for pending approvals.
 * Calls `onApprovals` with the full pending-approval snapshot on each cycle.
 */
export function startIronagentApprovalPolling(
  onApprovals: (approvals: ExecApprovalRequest[]) => void,
): () => void {
  pollListener = onApprovals;
  pollingActive = true;
  scheduleNextPoll(0);
  return () => stopIronagentApprovalPolling();
}

/** Trigger an immediate poll cycle without waiting for the next interval. */
export function triggerIronagentApprovalRefresh() {
  scheduleNextPoll(0);
}

export function stopIronagentApprovalPolling() {
  pollingActive = false;
  pollListener = null;
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}
