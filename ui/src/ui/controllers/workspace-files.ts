import { getSafeLocalStorage, getSafeSessionStorage } from "../../local-storage.ts";
import { resolveCurrentAssistantSingleEntryRoute } from "../assistant-route.ts";
import type { AgentFileEntry, AgentsFilesListResult } from "../types.ts";

const API_URL_STORAGE_KEY = "ironagent.ironagent.apiUrl";
const CONTROL_API_URL_SESSION_KEY = "ironagent.ironagent.controlApiUrl.session";
const ADMIN_TOKEN_STORAGE_KEY = "ironagent.ironagent.adminToken";
const LEGACY_ADMIN_TOKEN_STORAGE_KEY = ADMIN_TOKEN_STORAGE_KEY;
const ADMIN_TOKEN_SESSION_KEY = "ironagent.ironagent.adminToken.session";
const ASSISTANT_ID_STORAGE_KEY = "ironagent.ironagent.assistantId";
const ASSISTANT_ID_SESSION_KEY = "ironagent.ironagent.assistantId.session";
const API_URL_PARAM_KEYS = ["ironagentApiUrl", "ironAgentApiUrl"] as const;
const ASSISTANT_ID_PARAM_KEYS = ["ironagentAssistantId", "assistantId"] as const;

type WorkspaceFileApiEntry = {
  file_id: string;
  assistant_id: string;
  bucket: string;
  filename: string;
  relative_path: string;
  size_bytes: number;
  content_type: string;
  source_type: string;
  source_ref: string;
  status?: string;
  storage_status?: string;
  sync_status?: string;
  updated_at: string;
  deleted: boolean;
};

type WorkspaceFileListApiResponse = {
  files: WorkspaceFileApiEntry[];
  total: number;
};

type WorkspaceFileUploadApiResponse = {
  status: "accepted" | "pending_approval" | "rejected";
  file_id?: string | null;
  push_id?: string | null;
  approval_id?: string | null;
  assistant_id: string;
  filename: string;
  bucket: "temp" | "persistent";
  relative_path?: string | null;
  message?: string;
};

type WorkspaceFileDeleteApiResponse = {
  status: "deleted" | "pending_approval";
  file_id: string;
  assistant_id: string;
  relative_path: string;
  bucket?: "temp" | "persistent";
  approval_id?: string | null;
  message?: string;
};

type ControlUiAssistantContextAssistant = {
  id?: string | null;
  status?: string | null;
};

type ControlUiAssistantContextResponse = {
  assistants?: ControlUiAssistantContextAssistant[];
};

export type WorkspaceUploadResult = {
  status: WorkspaceFileUploadApiResponse["status"];
  fileId?: string;
  approvalId?: string | null;
  assistantId: string;
  filename: string;
  bucket: "temp" | "persistent";
  path: string;
  downloadUrl?: string;
  message?: string;
};

export type WorkspaceUploadOptions = {
  targetBucket?: "temp" | "persistent";
  sourceType?: string;
  sourceRef?: string;
};

export type WorkspaceDeleteResult = {
  status: WorkspaceFileDeleteApiResponse["status"];
  fileId: string;
  assistantId: string;
  path: string;
  bucket?: "temp" | "persistent";
  approvalId?: string | null;
  message?: string;
};

export type WorkspaceFilesState = {
  workspaceFilesLoading: boolean;
  workspaceFilesError: string | null;
  workspaceFilesList: AgentsFilesListResult | null;
};

export type LoadWorkspaceFilesOptions = {
  background?: boolean;
  sync?: boolean;
};

const WORKBENCH_BUCKETS = new Set(["temp", "persistent"]);
const WORKSPACE_FILES_FETCH_RETRY_DELAY_MS = 350;
const MISSING_WORKSPACE_UPLOAD_CREDENTIAL_MESSAGE =
  "当前页面缺少工作区上传凭证，请从后台重新打开聊天页。";

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function normalizeWorkspaceAdminTokenCandidate(value: string): string {
  const normalized = value.trim();
  if (!normalized) {
    return "";
  }
  const lowered = normalized.toLowerCase();
  if (lowered === "change-me" || lowered === "changeme") {
    return "";
  }
  return normalized;
}

function isGenericBrowserFetchFailure(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return /^(?:typeerror:\s*)?(?:fetch failed|failed to fetch)$/i.test(message.trim());
}

async function delayWorkspaceFilesRetry(ms: number): Promise<void> {
  await new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

function getQueryParam(name: string): string {
  if (typeof window === "undefined") {
    return "";
  }
  try {
    return new URL(window.location.href).searchParams.get(name)?.trim() ?? "";
  } catch {
    return "";
  }
}

function getHashParam(name: string): string {
  if (typeof window === "undefined") {
    return "";
  }
  try {
    const rawHash = window.location.hash.startsWith("#")
      ? window.location.hash.slice(1)
      : window.location.hash;
    return new URLSearchParams(rawHash).get(name)?.trim() ?? "";
  } catch {
    return "";
  }
}

export function applyWorkspaceFilesSettingsFromUrl() {
  if (typeof window === "undefined" || (!window.location.search && !window.location.hash)) {
    return;
  }
  const storage = getSafeLocalStorage();
  const url = new URL(window.location.href);
  const params = new URLSearchParams(url.search);
  const hashParams = new URLSearchParams(url.hash.startsWith("#") ? url.hash.slice(1) : url.hash);
  const singleEntryRoute = resolveCurrentAssistantSingleEntryRoute();
  let changed = false;

  const apiUrl = firstUrlParam(params, hashParams, API_URL_PARAM_KEYS);
  if (apiUrl && !singleEntryRoute) {
    getSafeSessionStorage()?.setItem(
      CONTROL_API_URL_SESSION_KEY,
      normalizeControlUiApiBaseUrl(apiUrl),
    );
    storage?.setItem(API_URL_STORAGE_KEY, normalizeLocalWorkspaceApiBaseUrl(apiUrl));
  }
  if (deleteUrlParams(params, hashParams, API_URL_PARAM_KEYS)) {
    changed = true;
  }

  // Prefer fragment tokens. Query-param tokens remain a legacy bridge only, and
  // are scrubbed immediately so they do not linger in history or referrers.
  const adminToken = (
    hashParams.get("ironagentAdminToken") ??
    params.get("ironagentAdminToken") ??
    ""
  ).trim();
  if (adminToken && !singleEntryRoute) {
    storeWorkspaceFilesAdminToken(adminToken);
  }
  if (params.has("ironagentAdminToken") || hashParams.has("ironagentAdminToken")) {
    params.delete("ironagentAdminToken");
    hashParams.delete("ironagentAdminToken");
    changed = true;
  }

  const assistantId = ASSISTANT_ID_PARAM_KEYS.map(
    (key) => hashParams.get(key) ?? params.get(key) ?? "",
  )
    .find((value) => value.trim())
    ?.trim();
  if (assistantId) {
    storeWorkspaceFilesAssistantId(assistantId);
  }
  for (const key of ASSISTANT_ID_PARAM_KEYS) {
    if (params.has(key) || hashParams.has(key)) {
      params.delete(key);
      hashParams.delete(key);
      changed = true;
    }
  }

  if (!changed) {
    return;
  }
  url.search = params.toString();
  const nextHash = hashParams.toString();
  url.hash = nextHash ? `#${nextHash}` : "";
  window.history.replaceState({}, "", url.toString());
}

export function storeWorkspaceFilesAdminToken(adminToken: string) {
  const normalized = normalizeWorkspaceAdminTokenCandidate(adminToken);
  const storage = getSafeLocalStorage();
  const session = getSafeSessionStorage();
  if (!normalized) {
    session?.removeItem(ADMIN_TOKEN_SESSION_KEY);
    storage?.removeItem(LEGACY_ADMIN_TOKEN_STORAGE_KEY);
    return;
  }
  session?.setItem(ADMIN_TOKEN_SESSION_KEY, normalized);
  storage?.removeItem(LEGACY_ADMIN_TOKEN_STORAGE_KEY);
}

export function storeWorkspaceFilesAssistantId(assistantId?: string | null) {
  const normalized = assistantId?.trim() ?? "";
  const session = getSafeSessionStorage();
  const storage = getSafeLocalStorage();
  if (!normalized) {
    session?.removeItem(ASSISTANT_ID_SESSION_KEY);
    storage?.removeItem(ASSISTANT_ID_STORAGE_KEY);
    return;
  }
  session?.setItem(ASSISTANT_ID_SESSION_KEY, normalized);
  storage?.setItem(ASSISTANT_ID_STORAGE_KEY, normalized);
}

export function resolveWorkspaceFilesAssistantId(assistantId: string): string {
  const normalized = assistantId.trim();
  const routeAssistantId = resolveCurrentAssistantSingleEntryRoute()?.assistantId;
  if (routeAssistantId) {
    return routeAssistantId;
  }
  const stored =
    getSafeSessionStorage()?.getItem(ASSISTANT_ID_SESSION_KEY)?.trim() ??
    getSafeLocalStorage()?.getItem(ASSISTANT_ID_STORAGE_KEY)?.trim() ??
    "";
  if (!stored) {
    return normalized;
  }
  if (!normalized || normalized === "main") {
    return stored;
  }
  return normalized;
}

async function inferAssistantIdFromAssistantContext(currentAssistantId: string): Promise<string> {
  const apiBaseUrl = resolveWorkspaceFilesApiBaseUrl();
  if (!apiBaseUrl || typeof fetch !== "function") {
    return currentAssistantId;
  }
  try {
    const url = buildWorkspaceFilesApiUrl(apiBaseUrl, "/api/v1/control-ui/assistant-context");
    const res = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) {
      return currentAssistantId;
    }
    const payload = (await res.json()) as ControlUiAssistantContextResponse;
    const assistants = (payload.assistants ?? [])
      .map((assistant) => ({
        id: assistant.id?.trim() ?? "",
        status: assistant.status?.trim() ?? "",
      }))
      .filter((assistant) => assistant.id);
    const preferred =
      assistants.length === 1
        ? assistants[0]
        : assistants.filter((assistant) => assistant.status === "running").length === 1
          ? assistants.filter((assistant) => assistant.status === "running")[0]
          : null;
    if (!preferred?.id) {
      return currentAssistantId;
    }
    storeWorkspaceFilesAssistantId(preferred.id);
    return preferred.id;
  } catch {
    return currentAssistantId;
  }
}

async function resolveWorkspaceFilesAssistantIdForRequest(assistantId: string): Promise<string> {
  let resolved = resolveWorkspaceFilesAssistantId(assistantId);
  if (resolved && resolved !== "main") {
    return resolved;
  }
  const { resolveControlUiLaunchTicket } = await import("./control-ui-launch.ts");
  const launch = await resolveControlUiLaunchTicket();
  resolved = resolveWorkspaceFilesAssistantId(assistantId);
  if (resolved && resolved !== "main") {
    return resolved;
  }
  if (launch.status === "resolved" && launch.assistantId?.trim()) {
    storeWorkspaceFilesAssistantId(launch.assistantId);
    return launch.assistantId.trim();
  }
  return await inferAssistantIdFromAssistantContext(resolved || assistantId.trim());
}

function getViteEnv(name: string): string {
  const meta = import.meta as ImportMeta & { env?: Record<string, string | undefined> };
  return normalizeWorkspaceAdminTokenCandidate(meta.env?.[name]?.trim() ?? "");
}

function getRuntimeInjectedValue(
  name: "__IRONAGENT_API_URL__" | "__IRONAGENT_ADMIN_TOKEN__",
): string {
  const runtimeValue = (globalThis as Record<string, unknown>)[name];
  return typeof runtimeValue === "string"
    ? name === "__IRONAGENT_ADMIN_TOKEN__"
      ? normalizeWorkspaceAdminTokenCandidate(runtimeValue)
      : runtimeValue.trim()
    : "";
}

function getCurrentLocationHost(): { hostname: string; protocol: string } {
  if (typeof window === "undefined") {
    return { hostname: "127.0.0.1", protocol: "http:" };
  }
  const hostname = window.location.hostname || "127.0.0.1";
  return {
    hostname: hostname === "0.0.0.0" ? "127.0.0.1" : hostname,
    protocol: window.location.protocol === "https:" ? "https:" : "http:",
  };
}

function getCurrentBrowserOrigin(): string {
  if (typeof window === "undefined") {
    return "http://127.0.0.1";
  }
  const { hostname, protocol } = getCurrentLocationHost();
  const port = window.location.port?.trim() ?? "";
  return `${protocol}//${hostname}${port ? `:${port}` : ""}`;
}

function isLocalBrowserHost(hostname: string): boolean {
  return hostname === "127.0.0.1" || hostname === "localhost";
}

function isPrivateWorkspaceBridgeHost(hostname: string): boolean {
  if (!hostname) {
    return false;
  }
  if (hostname === "host.docker.internal") {
    return true;
  }
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(hostname)) {
    return true;
  }
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(hostname)) {
    return true;
  }
  const match172 = /^172\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/.exec(hostname);
  if (!match172) {
    return false;
  }
  const secondOctet = Number.parseInt(match172[1] ?? "", 10);
  return Number.isFinite(secondOctet) && secondOctet >= 16 && secondOctet <= 31;
}

function firstUrlParam(
  params: URLSearchParams,
  hashParams: URLSearchParams,
  keys: readonly string[],
): string {
  for (const key of keys) {
    const value = (hashParams.get(key) ?? params.get(key) ?? "").trim();
    if (value) {
      return value;
    }
  }
  return "";
}

function deleteUrlParams(
  params: URLSearchParams,
  hashParams: URLSearchParams,
  keys: readonly string[],
): boolean {
  let changed = false;
  for (const key of keys) {
    if (params.has(key) || hashParams.has(key)) {
      params.delete(key);
      hashParams.delete(key);
      changed = true;
    }
  }
  return changed;
}

function normalizeLocalWorkspaceApiBaseUrl(candidate: string): string {
  const trimmed = trimTrailingSlash(candidate);
  if (!trimmed) {
    return "";
  }
  const { hostname } = getCurrentLocationHost();
  if (!isLocalBrowserHost(hostname)) {
    return trimmed;
  }
  try {
    const parsed = new URL(trimmed);
    const parsedHost = parsed.hostname === "0.0.0.0" ? "127.0.0.1" : parsed.hostname;
    if (isLocalBrowserHost(parsedHost) || isPrivateWorkspaceBridgeHost(parsedHost)) {
      return getCurrentBrowserOrigin();
    }
    return trimmed;
  } catch {
    return trimmed;
  }
}

function normalizeControlUiApiBaseUrl(candidate: string): string {
  const trimmed = trimTrailingSlash(candidate);
  if (!trimmed) {
    return "";
  }
  const { hostname } = getCurrentLocationHost();
  if (!isLocalBrowserHost(hostname)) {
    return trimmed;
  }
  try {
    const parsed = new URL(trimmed);
    const parsedHost = parsed.hostname === "0.0.0.0" ? "127.0.0.1" : parsed.hostname;
    if (isLocalBrowserHost(parsedHost)) {
      return trimTrailingSlash(parsed.toString());
    }
    if (isPrivateWorkspaceBridgeHost(parsedHost)) {
      parsed.hostname = hostname === "localhost" ? "localhost" : "127.0.0.1";
      return trimTrailingSlash(parsed.toString());
    }
    return trimmed;
  } catch {
    return trimmed;
  }
}

export function resolveControlUiApiBaseUrl(): string {
  const fromUrl =
    getHashParam("ironagentApiUrl") ||
    getQueryParam("ironagentApiUrl") ||
    getHashParam("ironAgentApiUrl") ||
    getQueryParam("ironAgentApiUrl");
  applyWorkspaceFilesSettingsFromUrl();
  const singleEntryRoute = resolveCurrentAssistantSingleEntryRoute();
  if (singleEntryRoute) {
    return singleEntryRoute.apiBasePath;
  }
  const storage = getSafeLocalStorage();
  const session = getSafeSessionStorage();
  if (fromUrl) {
    return normalizeControlUiApiBaseUrl(fromUrl);
  }
  const fromSession = session?.getItem(CONTROL_API_URL_SESSION_KEY)?.trim() ?? "";
  if (fromSession) {
    return normalizeControlUiApiBaseUrl(fromSession);
  }
  const runtimeInjected = getRuntimeInjectedValue("__IRONAGENT_API_URL__");
  if (runtimeInjected) {
    return normalizeControlUiApiBaseUrl(runtimeInjected);
  }
  const fromEnv = getViteEnv("VITE_IRONAGENT_API_URL");
  if (fromEnv) {
    return normalizeControlUiApiBaseUrl(fromEnv);
  }
  const fromStorage = storage?.getItem(API_URL_STORAGE_KEY)?.trim() ?? "";
  if (fromStorage) {
    return normalizeControlUiApiBaseUrl(fromStorage);
  }
  const { hostname } = getCurrentLocationHost();
  if (isLocalBrowserHost(hostname)) {
    return getCurrentBrowserOrigin();
  }
  return "";
}

export function resolveWorkspaceFilesApiBaseUrl(): string {
  applyWorkspaceFilesSettingsFromUrl();
  const singleEntryRoute = resolveCurrentAssistantSingleEntryRoute();
  if (singleEntryRoute) {
    return singleEntryRoute.apiBasePath;
  }
  const storage = getSafeLocalStorage();
  const fromUrl =
    getHashParam("ironagentApiUrl") ||
    getQueryParam("ironagentApiUrl") ||
    getHashParam("ironAgentApiUrl") ||
    getQueryParam("ironAgentApiUrl");
  if (fromUrl) {
    const normalized = normalizeLocalWorkspaceApiBaseUrl(fromUrl);
    if (normalized !== trimTrailingSlash(fromUrl)) {
      storage?.setItem(API_URL_STORAGE_KEY, normalized);
    }
    return normalized;
  }
  const runtimeInjected = getRuntimeInjectedValue("__IRONAGENT_API_URL__");
  if (runtimeInjected) {
    return normalizeLocalWorkspaceApiBaseUrl(runtimeInjected);
  }
  const fromEnv = getViteEnv("VITE_IRONAGENT_API_URL");
  if (fromEnv) {
    return normalizeLocalWorkspaceApiBaseUrl(fromEnv);
  }
  const fromStorage = storage?.getItem(API_URL_STORAGE_KEY)?.trim() ?? "";
  if (fromStorage) {
    const normalized = normalizeLocalWorkspaceApiBaseUrl(fromStorage);
    if (isCrossOriginApiBaseUrl(normalized)) {
      storage?.removeItem(API_URL_STORAGE_KEY);
    } else {
      if (normalized !== trimTrailingSlash(fromStorage)) {
        storage?.setItem(API_URL_STORAGE_KEY, normalized);
      }
      return normalized;
    }
  }
  const { hostname } = getCurrentLocationHost();
  if (isLocalBrowserHost(hostname)) {
    return getCurrentBrowserOrigin();
  }
  return "";
}

function isCrossOriginApiBaseUrl(candidate: string): boolean {
  if (!candidate || candidate.startsWith("/")) {
    return false;
  }
  if (typeof window === "undefined") {
    return false;
  }
  const { hostname } = getCurrentLocationHost();
  if (isLocalBrowserHost(hostname)) {
    return false;
  }
  try {
    return new URL(candidate, window.location.href).origin !== window.location.origin;
  } catch {
    return false;
  }
}

function resolveAdminToken(): string {
  applyWorkspaceFilesSettingsFromUrl();
  const storage = getSafeLocalStorage();
  const session = getSafeSessionStorage();
  const fromUrl = normalizeWorkspaceAdminTokenCandidate(
    getHashParam("ironagentAdminToken") || getQueryParam("ironagentAdminToken"),
  );
  if (fromUrl) {
    return fromUrl;
  }
  const runtimeInjected = getRuntimeInjectedValue("__IRONAGENT_ADMIN_TOKEN__");
  if (runtimeInjected) {
    return runtimeInjected;
  }
  const fromEnv = getViteEnv("VITE_IRONAGENT_ADMIN_TOKEN");
  if (fromEnv) {
    return fromEnv;
  }
  const fromSession = normalizeWorkspaceAdminTokenCandidate(
    session?.getItem(ADMIN_TOKEN_SESSION_KEY)?.trim() ?? "",
  );
  if (fromSession) {
    return fromSession;
  }
  const legacyFromStorage = normalizeWorkspaceAdminTokenCandidate(
    storage?.getItem(LEGACY_ADMIN_TOKEN_STORAGE_KEY)?.trim() ?? "",
  );
  if (legacyFromStorage) {
    session?.setItem(ADMIN_TOKEN_SESSION_KEY, legacyFromStorage);
    storage?.removeItem(LEGACY_ADMIN_TOKEN_STORAGE_KEY);
    return legacyFromStorage;
  }
  return "";
}

export function resolveWorkspaceFilesAdminToken(): string {
  return resolveAdminToken();
}

export function shouldUseWorkspaceFilesBrowserAdminToken(apiBaseUrl: string): boolean {
  return Boolean(apiBaseUrl && !apiBaseUrl.startsWith("/"));
}

async function resolveAdminTokenForMutation(apiBaseUrl: string): Promise<string> {
  if (!shouldUseWorkspaceFilesBrowserAdminToken(apiBaseUrl)) {
    return "";
  }
  let token = resolveAdminToken();
  if (token) {
    return token;
  }
  const { resolveControlUiLaunchTicket } = await import("./control-ui-launch.ts");
  const launch = await resolveControlUiLaunchTicket();
  token = resolveAdminToken();
  if (token) {
    return token;
  }
  if (launch.status === "failed") {
    throw new Error(launch.message);
  }
  throw new Error(MISSING_WORKSPACE_UPLOAD_CREDENTIAL_MESSAGE);
}

export function buildWorkspaceFilesApiUrl(apiBaseUrl: string, path: string): string {
  return `${apiBaseUrl}${path}`;
}

function workspaceFileContentUrl(
  apiBaseUrl: string,
  fileId: string,
  disposition: "attachment" | "inline",
): string {
  return buildWorkspaceFilesApiUrl(
    apiBaseUrl,
    `/api/v1/workspace-files/${encodeURIComponent(fileId)}/download?disposition=${disposition}`,
  );
}

function workspaceFilePath(
  entry: Pick<WorkspaceFileApiEntry, "bucket" | "filename" | "relative_path">,
) {
  return entry.relative_path || `${entry.bucket}/${entry.filename}`;
}

function isWorkspaceFileMissing(file: WorkspaceFileApiEntry): boolean {
  if (file.deleted) {
    return true;
  }
  return (
    file.status === "missing" ||
    file.storage_status === "deleted_from_workspace" ||
    file.sync_status === "deleted_from_workspace" ||
    file.sync_status === "missing_from_workspace"
  );
}

function mapWorkspaceFile(apiBaseUrl: string, file: WorkspaceFileApiEntry): AgentFileEntry {
  const bucket = file.bucket === "persistent" ? "persistent" : "temp";
  return {
    name: file.filename,
    path: workspaceFilePath(file),
    missing: isWorkspaceFileMissing(file),
    size: file.size_bytes,
    updatedAtMs: Date.parse(file.updated_at),
    fileId: file.file_id,
    bucket,
    contentType: file.content_type,
    viewUrl: workspaceFileContentUrl(apiBaseUrl, file.file_id, "inline"),
    downloadUrl: workspaceFileContentUrl(apiBaseUrl, file.file_id, "attachment"),
    sourceType: file.source_type,
    sourceRef: file.source_ref,
  };
}

async function readJsonError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown };
    const detail = body.detail ?? body.message;
    return typeof detail === "string" ? detail : `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

function isInvalidWorkspaceAdminTokenMessage(message: string): boolean {
  return /invalid admin token/i.test(message);
}

async function refreshWorkspaceMutationAdminToken(): Promise<string> {
  storeWorkspaceFilesAdminToken("");
  const { resolveControlUiLaunchTicket } = await import("./control-ui-launch.ts");
  const launch = await resolveControlUiLaunchTicket();
  const token = resolveWorkspaceFilesAdminToken();
  if (token) {
    return token;
  }
  if (launch.status === "failed") {
    throw new Error(launch.message);
  }
  return "";
}

async function sendWorkspaceMutationRequest(
  apiBaseUrl: string,
  path: string,
  init: {
    method: "POST" | "DELETE";
    body?: BodyInit;
  },
): Promise<Response> {
  const url = buildWorkspaceFilesApiUrl(apiBaseUrl, path);
  let token = await resolveAdminTokenForMutation(apiBaseUrl);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const headers = token
      ? {
          IRONAGENT_ADMIN_TOKEN: token,
          "X-Admin-Token": token,
        }
      : undefined;
    const res = await fetch(url, {
      method: init.method,
      headers,
      body: init.body,
    });
    if (res.ok) {
      return res;
    }
    const detail = await readJsonError(res);
    if (attempt === 0 && isInvalidWorkspaceAdminTokenMessage(detail)) {
      token = await refreshWorkspaceMutationAdminToken();
      continue;
    }
    throw new Error(`${url}: ${detail}`);
  }
  throw new Error(`${url}: request failed`);
}

export async function loadWorkspaceFiles(
  state: WorkspaceFilesState,
  assistantId: string,
  options: LoadWorkspaceFilesOptions = {},
) {
  const trimmedAssistantId = await resolveWorkspaceFilesAssistantIdForRequest(assistantId);
  if (!trimmedAssistantId || state.workspaceFilesLoading) {
    return;
  }
  const background = options.background === true;
  if (!background) {
    state.workspaceFilesLoading = true;
    state.workspaceFilesError = null;
  }
  const apiBaseUrl = resolveWorkspaceFilesApiBaseUrl();
  try {
    const params = new URLSearchParams({ assistant_id: trimmedAssistantId });
    if (options.sync === true) {
      params.set("sync", "true");
    }
    let data: WorkspaceFileListApiResponse | null = null;
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const url = buildWorkspaceFilesApiUrl(
          apiBaseUrl,
          `/api/v1/workspace-files?${params.toString()}`,
        );
        const res = await fetch(url, {
          method: "GET",
        });
        if (!res.ok) {
          throw new Error(`${url}: ${await readJsonError(res)}`);
        }
        data = (await res.json()) as WorkspaceFileListApiResponse;
        lastError = null;
        break;
      } catch (err) {
        lastError = err;
        if (attempt === 0 && isGenericBrowserFetchFailure(err)) {
          await delayWorkspaceFilesRetry(WORKSPACE_FILES_FETCH_RETRY_DELAY_MS);
          continue;
        }
        throw err instanceof TypeError
          ? new Error(
              `${buildWorkspaceFilesApiUrl(
                apiBaseUrl,
                `/api/v1/workspace-files?${params.toString()}`,
              )}: ${err.message}`,
            )
          : err;
      }
    }
    if (!data) {
      throw lastError instanceof Error ? lastError : new Error(String(lastError));
    }
    const workbenchFiles = data.files.filter((file) => WORKBENCH_BUCKETS.has(file.bucket));
    state.workspaceFilesList = {
      agentId: trimmedAssistantId,
      workspace: `${trimmedAssistantId} persistent workspace`,
      files: workbenchFiles.map((file) => mapWorkspaceFile(apiBaseUrl, file)),
    };
    state.workspaceFilesError = null;
  } catch (err) {
    state.workspaceFilesError = String(err);
  } finally {
    if (!background) {
      state.workspaceFilesLoading = false;
    }
  }
}

export async function uploadWorkspaceFiles(
  state: WorkspaceFilesState,
  assistantId: string,
  files: File[],
  options?: WorkspaceUploadOptions,
): Promise<WorkspaceUploadResult[]> {
  const trimmedAssistantId = await resolveWorkspaceFilesAssistantIdForRequest(assistantId);
  if (!trimmedAssistantId || files.length === 0) {
    return [];
  }
  state.workspaceFilesLoading = true;
  state.workspaceFilesError = null;
  const apiBaseUrl = resolveWorkspaceFilesApiBaseUrl();
  const targetBucket = options?.targetBucket ?? "persistent";
  const sourceType = options?.sourceType?.trim() || "webui-chat";
  const sourceRef = options?.sourceRef?.trim() || "chat-send";
  try {
    const results: WorkspaceUploadResult[] = [];
    for (const file of files) {
      const form = new FormData();
      form.append("file", file, file.name);
      form.append("assistant_id", trimmedAssistantId);
      form.append("target_bucket", targetBucket);
      form.append("source_type", sourceType);
      form.append("source_ref", sourceRef);
      const res = await sendWorkspaceMutationRequest(apiBaseUrl, "/api/v1/workspace-files/upload", {
        method: "POST",
        body: form,
      });
      const data = (await res.json()) as WorkspaceFileUploadApiResponse;
      const path = data.relative_path?.trim() || `${data.bucket}/${data.filename}`;
      results.push({
        status: data.status,
        fileId: data.file_id ?? undefined,
        approvalId: data.approval_id,
        assistantId: data.assistant_id,
        filename: data.filename,
        bucket: data.bucket,
        path,
        downloadUrl: data.file_id
          ? workspaceFileContentUrl(apiBaseUrl, data.file_id, "attachment")
          : undefined,
        message: data.message,
      });
    }
    state.workspaceFilesLoading = false;
    await loadWorkspaceFiles(state, trimmedAssistantId);
    return results;
  } catch (err) {
    const message = String(err);
    state.workspaceFilesError = message.includes(MISSING_WORKSPACE_UPLOAD_CREDENTIAL_MESSAGE)
      ? null
      : message;
    throw err;
  } finally {
    state.workspaceFilesLoading = false;
  }
}

export async function deleteWorkspaceFile(
  state: WorkspaceFilesState,
  assistantId: string,
  fileId: string,
): Promise<WorkspaceDeleteResult | null> {
  const trimmedFileId = fileId.trim();
  if (!trimmedFileId) {
    return null;
  }
  const trimmedAssistantId = await resolveWorkspaceFilesAssistantIdForRequest(assistantId);
  if (!trimmedAssistantId) {
    return null;
  }
  state.workspaceFilesLoading = true;
  state.workspaceFilesError = null;
  const apiBaseUrl = resolveWorkspaceFilesApiBaseUrl();
  try {
    const res = await sendWorkspaceMutationRequest(
      apiBaseUrl,
      `/api/v1/workspace-files/${encodeURIComponent(trimmedFileId)}/delete`,
      {
        method: "POST",
      },
    );
    const data = (await res.json()) as WorkspaceFileDeleteApiResponse;
    state.workspaceFilesLoading = false;
    await loadWorkspaceFiles(state, trimmedAssistantId);
    return {
      status: data.status,
      fileId: data.file_id,
      assistantId: data.assistant_id,
      path: data.relative_path,
      bucket: data.bucket,
      approvalId: data.approval_id,
      message: data.message,
    };
  } catch (err) {
    state.workspaceFilesError = String(err);
    throw err;
  } finally {
    state.workspaceFilesLoading = false;
  }
}
