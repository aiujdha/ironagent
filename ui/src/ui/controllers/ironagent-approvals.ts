import type { ExecApprovalRequest } from "./exec-approval.ts";
import {
  buildWorkspaceFilesApiUrl,
  resolveWorkspaceFilesAdminToken,
  resolveWorkspaceFilesApiBaseUrl,
  shouldUseWorkspaceFilesBrowserAdminToken,
} from "./workspace-files.ts";

export function isIronAgentApproval(entry: ExecApprovalRequest): boolean {
  return entry.kind === "plugin" && entry.pluginMetadata?.source === "ironagent";
}

export function canResolveGatewayApprovals(scopes: readonly string[] | null | undefined): boolean {
  if (!Array.isArray(scopes)) {
    return false;
  }
  return scopes.includes("operator.admin") || scopes.includes("operator.approvals");
}

export function isControlUiActionableApproval(
  entry: ExecApprovalRequest,
  opts?: { canResolveGatewayApprovals?: boolean },
): boolean {
  if (isIronAgentApproval(entry)) {
    return true;
  }
  return opts?.canResolveGatewayApprovals === true;
}

export async function resolveIronAgentApproval(
  entry: ExecApprovalRequest,
  decision: "allow-once" | "allow-always" | "deny",
): Promise<void> {
  if (!isIronAgentApproval(entry) || !entry.pluginMetadata?.approvalId) {
    return;
  }

  const apiBaseUrl = resolveWorkspaceFilesApiBaseUrl();
  const adminToken = resolveWorkspaceFilesAdminToken().trim();
  if (shouldUseWorkspaceFilesBrowserAdminToken(apiBaseUrl) && !adminToken) {
    throw new Error("IronAgent admin token unavailable");
  }

  const resolution = decision === "deny" ? "deny" : "allow";
  const url = buildWorkspaceFilesApiUrl(
    apiBaseUrl,
    `/api/v1/admin/approvals/${encodeURIComponent(entry.pluginMetadata.approvalId)}/resolve`,
  );
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(adminToken ? { "X-Admin-Token": adminToken } : {}),
    },
    body: JSON.stringify({ resolution }),
  });

  if (response.ok) {
    return;
  }

  let message = `${response.status} ${response.statusText}`;
  try {
    const payload = (await response.json()) as { detail?: unknown; message?: unknown };
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      message = payload.detail.trim();
    } else if (typeof payload.message === "string" && payload.message.trim()) {
      message = payload.message.trim();
    }
  } catch {
    // Keep the HTTP fallback message.
  }
  throw new Error(`IronAgent approval sync failed: ${message}`);
}
