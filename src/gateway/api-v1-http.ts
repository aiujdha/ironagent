import { randomUUID } from "node:crypto";
import fs from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import { isRequestBodyLimitError, readRequestBodyWithLimit } from "../infra/http-body.js";
import { detectMime } from "../media/mime.js";
import {
  extractOriginalFilename,
  MEDIA_MAX_BYTES,
  resolveMediaBufferPath,
  saveMediaBuffer,
} from "../media/store.js";
import { reconcileTaskLookupToken } from "../tasks/task-registry.maintenance.js";
import type { TaskRecord, TaskStatus } from "../tasks/task-registry.types.js";
import { formatTaskStatusDetail, formatTaskStatusTitle } from "../tasks/task-status.js";
import type { AuthRateLimiter } from "./auth-rate-limit.js";
import type { ResolvedGatewayAuth } from "./auth.js";
import { sendJson, sendMethodNotAllowed } from "./http-common.js";
import {
  authorizeGatewayHttpRequestOrReply,
  resolveOpenAiCompatibleHttpOperatorScopes,
  resolveOpenAiCompatibleHttpSenderIsOwner,
} from "./http-utils.js";
import { validateSessionsCreateParams } from "./protocol/index.js";
import type { ErrorShape } from "./protocol/index.js";
import { ErrorCodes } from "./protocol/schema/error-codes.js";
import { sessionsHandlers } from "./server-methods/sessions.js";
import { toolsCatalogHandlers } from "./server-methods/tools-catalog.js";
import type {
  GatewayClient,
  GatewayRequestContext,
  GatewayRequestHandler,
} from "./server-methods/types.js";

const API_V1_PREFIX = "/api/v1";
const FILES_SUBDIR = "ironagent";
const DEFAULT_JSON_BODY_BYTES = 256 * 1024;

type ApiV1HttpOptions = {
  auth: ResolvedGatewayAuth;
  trustedProxies?: string[];
  allowRealIpFallback?: boolean;
  rateLimiter?: AuthRateLimiter;
  getGatewayRequestContext?: () => GatewayRequestContext | null;
};

type ApiGatewayRequest = {
  client: GatewayClient;
  context: GatewayRequestContext;
};

type InvokeGatewayHandlerResult =
  | {
      ok: true;
      payload: unknown;
      meta?: Record<string, unknown>;
    }
  | {
      ok: false;
      error?: ErrorShape;
      meta?: Record<string, unknown>;
    };

type UploadPayload =
  | {
      buffer: Buffer;
      contentType?: string;
      filename?: string;
    }
  | {
      error: string;
      status: number;
    };

const isWebchatConnect = () => false;

async function readBodyWithLimit(
  req: IncomingMessage,
  options: Parameters<typeof readRequestBodyWithLimit>[1],
): Promise<
  | { ok: true; body: string }
  | { ok: false; status: 408 | 413; code: "request_timeout" | "payload_too_large"; message: string }
> {
  try {
    return {
      ok: true,
      body: await readRequestBodyWithLimit(req, options),
    };
  } catch (error) {
    if (isRequestBodyLimitError(error, "PAYLOAD_TOO_LARGE")) {
      return {
        ok: false,
        status: 413,
        code: "payload_too_large",
        message: "Request body is too large.",
      };
    }
    if (isRequestBodyLimitError(error, "REQUEST_BODY_TIMEOUT")) {
      return {
        ok: false,
        status: 408,
        code: "request_timeout",
        message: "Request body timeout.",
      };
    }
    throw error;
  }
}

function sendApiError(
  res: ServerResponse,
  status: number,
  code: string,
  message: string,
  details?: Record<string, unknown>,
) {
  sendJson(res, status, {
    ok: false,
    error: {
      code,
      message,
      ...(details ? { details } : {}),
    },
  });
}

function mapGatewayErrorStatus(error?: ErrorShape): number {
  switch (error?.code) {
    case ErrorCodes.INVALID_REQUEST:
      return 400;
    case ErrorCodes.APPROVAL_NOT_FOUND:
      return 404;
    case ErrorCodes.NOT_LINKED:
    case ErrorCodes.NOT_PAIRED:
      return 409;
    case ErrorCodes.AGENT_TIMEOUT:
    case ErrorCodes.UNAVAILABLE:
      return 503;
    default:
      return 400;
  }
}

function decodePathToken(value: string): string | null {
  try {
    const decoded = decodeURIComponent(value).trim();
    return decoded || null;
  } catch {
    return null;
  }
}

function toIsoTime(value: number | undefined): string | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? new Date(value).toISOString()
    : undefined;
}

function mapTaskStatus(status: TaskStatus): "pending" | "completed" | "rejected" {
  if (status === "queued" || status === "running") {
    return "pending";
  }
  if (status === "succeeded") {
    return "completed";
  }
  return "rejected";
}

function buildTaskResponse(task: TaskRecord, lookupToken: string) {
  const relatedSessionKey = task.childSessionKey?.trim() || task.ownerKey.trim() || undefined;
  return {
    ok: true,
    task_id: task.taskId,
    lookup_token: lookupToken,
    status: mapTaskStatus(task.status),
    raw_status: task.status,
    title: formatTaskStatusTitle(task),
    detail: formatTaskStatusDetail(task),
    runtime: task.runtime,
    run_id: task.runId,
    session_key: relatedSessionKey,
    owner_key: task.ownerKey,
    agent_id: task.agentId,
    label: task.label,
    task: task.task,
    delivery_status: task.deliveryStatus,
    terminal_outcome: task.terminalOutcome,
    progress_summary: task.progressSummary,
    terminal_summary: task.terminalSummary,
    error: task.error,
    created_at: toIsoTime(task.createdAt),
    started_at: toIsoTime(task.startedAt),
    ended_at: toIsoTime(task.endedAt),
    last_event_at: toIsoTime(task.lastEventAt),
  };
}

function parseBooleanParam(raw: string | null | undefined): boolean | undefined {
  if (raw == null) {
    return undefined;
  }
  const normalized = raw.trim().toLowerCase();
  if (!normalized) {
    return undefined;
  }
  if (normalized === "true" || normalized === "1" || normalized === "yes") {
    return true;
  }
  if (normalized === "false" || normalized === "0" || normalized === "no") {
    return false;
  }
  return undefined;
}

async function authorizeApiGatewayRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<ApiGatewayRequest | null> {
  const requestAuth = await authorizeGatewayHttpRequestOrReply({
    req,
    res,
    auth: opts.auth,
    trustedProxies: opts.trustedProxies,
    allowRealIpFallback: opts.allowRealIpFallback,
    rateLimiter: opts.rateLimiter,
  });
  if (!requestAuth) {
    return null;
  }
  const context = opts.getGatewayRequestContext?.() ?? null;
  if (!context) {
    sendApiError(res, 503, "gateway_unavailable", "Gateway request context is not ready.");
    return null;
  }
  const scopes = resolveOpenAiCompatibleHttpOperatorScopes(req, requestAuth);
  const senderIsOwner = resolveOpenAiCompatibleHttpSenderIsOwner(req, requestAuth);
  const client: GatewayClient = {
    connect: {
      role: "operator",
      scopes,
    } as GatewayClient["connect"],
    internal: {
      allowModelOverride: senderIsOwner,
    },
  };
  return { client, context };
}

async function invokeGatewayHandler(params: {
  handler: GatewayRequestHandler;
  method: string;
  request: ApiGatewayRequest;
  payload: Record<string, unknown>;
}): Promise<InvokeGatewayHandlerResult> {
  let result: InvokeGatewayHandlerResult | null = null;
  await params.handler({
    req: {
      type: "req",
      id: randomUUID(),
      method: params.method,
      params: params.payload,
    },
    params: params.payload,
    client: params.request.client,
    isWebchatConnect,
    respond: (ok, payload, error, meta) => {
      if (result) {
        return;
      }
      result = ok
        ? {
            ok: true,
            payload,
            meta,
          }
        : {
            ok: false,
            error,
            meta,
          };
    },
    context: params.request.context,
  });
  if (result) {
    return result;
  }
  return {
    ok: false,
    error: {
      code: ErrorCodes.UNAVAILABLE,
      message: `Gateway method ${params.method} did not return a response.`,
    },
  };
}

function getRequestUrl(req: IncomingMessage): URL {
  const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
  url.pathname = stripAssistantApiPrefix(url.pathname);
  return url;
}

function stripAssistantApiPrefix(pathname: string): string {
  return pathname.replace(/^\/assistants\/[^/]+(?=\/api\/)/, "");
}

function resolveUploadFilename(req: IncomingMessage, fallback = "upload.bin"): string {
  const url = getRequestUrl(req);
  const fromQuery = url.searchParams.get("filename")?.trim();
  const fromHeader = req.headers["x-filename"]?.toString().trim();
  return fromQuery || fromHeader || fallback;
}

async function readUploadPayload(req: IncomingMessage): Promise<UploadPayload> {
  const contentTypeHeader = req.headers["content-type"]?.toString().trim();
  const bodyResult = await readBodyWithLimit(req, {
    maxBytes: MEDIA_MAX_BYTES,
    encoding: "latin1",
  });

  if (!bodyResult.ok) {
    return {
      error:
        bodyResult.code === "payload_too_large"
          ? `Uploaded file exceeds ${MEDIA_MAX_BYTES} bytes.`
          : bodyResult.message,
      status: bodyResult.status,
    };
  }
  const body = Buffer.from(bodyResult.body, "latin1");
  if (body.byteLength === 0) {
    return {
      error: "Request body is empty.",
      status: 400,
    };
  }

  if (contentTypeHeader?.toLowerCase().startsWith("multipart/form-data")) {
    try {
      const request = new Request("http://localhost/api/v1/files/upload", {
        method: "POST",
        headers: req.headers as Record<string, string>,
        body,
      });
      const form = await request.formData();
      const fileValue =
        form.get("file") ?? [...form.values()].find((value) => value instanceof Blob);
      if (!(fileValue instanceof Blob)) {
        return {
          error: "Expected a multipart field named `file`.",
          status: 400,
        };
      }
      const arrayBuffer = await fileValue.arrayBuffer();
      const filename =
        typeof (fileValue as { name?: unknown }).name === "string"
          ? (fileValue as { name: string }).name
          : undefined;
      return {
        buffer: Buffer.from(arrayBuffer),
        contentType: fileValue.type || undefined,
        filename,
      };
    } catch (error) {
      return {
        error: `Invalid multipart upload: ${error instanceof Error ? error.message : String(error)}`,
        status: 400,
      };
    }
  }

  return {
    buffer: body,
    contentType: contentTypeHeader,
    filename: resolveUploadFilename(req),
  };
}

function buildDownloadFilename(fileId: string): string {
  const extracted = extractOriginalFilename(fileId);
  return extracted || path.basename(fileId) || "download.bin";
}

async function handleApiHealthRequest(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
  const url = getRequestUrl(req);
  if (url.pathname !== `${API_V1_PREFIX}/health`) {
    return false;
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    sendMethodNotAllowed(res, "GET, HEAD");
    return true;
  }
  const body = JSON.stringify({ ok: true, status: "live" });
  res.statusCode = 200;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(req.method === "HEAD" ? undefined : body);
  return true;
}

async function handleApiToolsCatalogRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  if (url.pathname !== `${API_V1_PREFIX}/tools/catalog`) {
    return false;
  }
  if (req.method !== "GET") {
    sendMethodNotAllowed(res, "GET");
    return true;
  }
  const authorized = await authorizeApiGatewayRequest(req, res, opts);
  if (!authorized) {
    return true;
  }

  const includePlugins = parseBooleanParam(
    url.searchParams.get("include_plugins") ?? url.searchParams.get("includePlugins"),
  );
  const agentId = url.searchParams.get("agent_id") ?? url.searchParams.get("agentId");
  const result = await invokeGatewayHandler({
    handler: toolsCatalogHandlers["tools.catalog"],
    method: "tools.catalog",
    request: authorized,
    payload: {
      ...(typeof agentId === "string" && agentId.trim() ? { agentId: agentId.trim() } : {}),
      ...(typeof includePlugins === "boolean" ? { includePlugins } : {}),
    },
  });
  if (!result.ok) {
    sendApiError(
      res,
      mapGatewayErrorStatus(result.error),
      result.error?.code ?? "gateway_error",
      result.error?.message ?? "Failed to load tools catalog.",
    );
    return true;
  }

  const payload =
    result.payload && typeof result.payload === "object"
      ? (result.payload as Record<string, unknown>)
      : {};
  sendJson(res, 200, {
    ok: true,
    agent_id: payload.agentId,
    profiles: payload.profiles,
    groups: payload.groups,
  });
  return true;
}

async function handleApiSessionCreateRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  if (url.pathname !== `${API_V1_PREFIX}/sessions/create`) {
    return false;
  }
  if (req.method !== "POST") {
    sendMethodNotAllowed(res, "POST");
    return true;
  }
  const authorized = await authorizeApiGatewayRequest(req, res, opts);
  if (!authorized) {
    return true;
  }
  const bodyResult = await readBodyWithLimit(req, {
    maxBytes: DEFAULT_JSON_BODY_BYTES,
  });
  if (!bodyResult.ok) {
    sendApiError(res, bodyResult.status, bodyResult.code, bodyResult.message);
    return true;
  }
  let parsed: unknown = {};
  try {
    parsed = bodyResult.body.trim() ? (JSON.parse(bodyResult.body) as unknown) : {};
  } catch (error) {
    sendApiError(
      res,
      400,
      "invalid_json",
      error instanceof Error ? error.message : "Invalid JSON body.",
    );
    return true;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    sendApiError(res, 400, "invalid_request", "Request body must be a JSON object.");
    return true;
  }
  if (!validateSessionsCreateParams(parsed)) {
    sendApiError(res, 400, "invalid_request", "Invalid sessions.create payload.");
    return true;
  }

  const result = await invokeGatewayHandler({
    handler: sessionsHandlers["sessions.create"],
    method: "sessions.create",
    request: authorized,
    payload: parsed as Record<string, unknown>,
  });
  if (!result.ok) {
    sendApiError(
      res,
      mapGatewayErrorStatus(result.error),
      result.error?.code ?? "gateway_error",
      result.error?.message ?? "Failed to create session.",
    );
    return true;
  }

  const payload =
    result.payload && typeof result.payload === "object"
      ? (result.payload as Record<string, unknown>)
      : {};
  sendJson(res, 200, {
    ok: true,
    session_id: payload.sessionId,
    session_key: payload.key,
    task_id: payload.runId,
    run_started: payload.runStarted,
    entry: payload.entry,
  });
  return true;
}

async function resolveSessionDeleteKey(params: {
  token: string;
  request: ApiGatewayRequest;
}): Promise<string | null> {
  const payload =
    params.token.includes(":") || params.token === "global" || params.token === "unknown"
      ? { key: params.token }
      : { sessionId: params.token };
  const resolved = await invokeGatewayHandler({
    handler: sessionsHandlers["sessions.resolve"],
    method: "sessions.resolve",
    request: params.request,
    payload,
  });
  if (!resolved.ok) {
    return null;
  }
  const resultPayload =
    resolved.payload && typeof resolved.payload === "object"
      ? (resolved.payload as Record<string, unknown>)
      : {};
  return typeof resultPayload.key === "string" ? resultPayload.key : null;
}

async function handleApiSessionDeleteRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  const match = url.pathname.match(/^\/api\/v1\/sessions\/([^/]+)$/);
  if (!match) {
    return false;
  }
  if (req.method !== "DELETE") {
    sendMethodNotAllowed(res, "DELETE");
    return true;
  }
  const token = decodePathToken(match[1] ?? "");
  if (!token) {
    sendApiError(res, 400, "invalid_request", "Invalid session identifier.");
    return true;
  }
  const authorized = await authorizeApiGatewayRequest(req, res, opts);
  if (!authorized) {
    return true;
  }
  const key = await resolveSessionDeleteKey({
    token,
    request: authorized,
  });
  if (!key) {
    sendApiError(res, 404, "not_found", `Session not found: ${token}`);
    return true;
  }
  const deleteTranscript = parseBooleanParam(url.searchParams.get("delete_transcript")) ?? true;
  const result = await invokeGatewayHandler({
    handler: sessionsHandlers["sessions.delete"],
    method: "sessions.delete",
    request: authorized,
    payload: {
      key,
      deleteTranscript,
    },
  });
  if (!result.ok) {
    sendApiError(
      res,
      mapGatewayErrorStatus(result.error),
      result.error?.code ?? "gateway_error",
      result.error?.message ?? "Failed to delete session.",
    );
    return true;
  }
  const payload =
    result.payload && typeof result.payload === "object"
      ? (result.payload as Record<string, unknown>)
      : {};
  sendJson(res, 200, {
    ok: true,
    session_key: payload.key ?? key,
    deleted: payload.deleted === true,
    archived: payload.archived,
  });
  return true;
}

async function handleApiTaskStatusRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  const match = url.pathname.match(/^\/api\/v1\/tasks\/([^/]+)$/);
  if (!match) {
    return false;
  }
  if (req.method !== "GET") {
    sendMethodNotAllowed(res, "GET");
    return true;
  }
  const lookupToken = decodePathToken(match[1] ?? "");
  if (!lookupToken) {
    sendApiError(res, 400, "invalid_request", "Invalid task identifier.");
    return true;
  }
  const requestAuth = await authorizeGatewayHttpRequestOrReply({
    req,
    res,
    auth: opts.auth,
    trustedProxies: opts.trustedProxies,
    allowRealIpFallback: opts.allowRealIpFallback,
    rateLimiter: opts.rateLimiter,
  });
  if (!requestAuth) {
    return true;
  }
  const task = reconcileTaskLookupToken(lookupToken);
  if (!task) {
    sendApiError(res, 404, "not_found", `Task not found: ${lookupToken}`);
    return true;
  }
  sendJson(res, 200, buildTaskResponse(task, lookupToken));
  return true;
}

async function handleApiFileUploadRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  if (url.pathname !== `${API_V1_PREFIX}/files/upload`) {
    return false;
  }
  if (req.method !== "POST") {
    sendMethodNotAllowed(res, "POST");
    return true;
  }
  const requestAuth = await authorizeGatewayHttpRequestOrReply({
    req,
    res,
    auth: opts.auth,
    trustedProxies: opts.trustedProxies,
    allowRealIpFallback: opts.allowRealIpFallback,
    rateLimiter: opts.rateLimiter,
  });
  if (!requestAuth) {
    return true;
  }
  const upload = await readUploadPayload(req);
  if ("error" in upload) {
    sendApiError(res, upload.status, "invalid_upload", upload.error);
    return true;
  }
  try {
    const saved = await saveMediaBuffer(
      upload.buffer,
      upload.contentType,
      FILES_SUBDIR,
      MEDIA_MAX_BYTES,
      upload.filename,
    );
    sendJson(res, 200, {
      ok: true,
      file_id: saved.id,
      filename: buildDownloadFilename(saved.id),
      size: saved.size,
      content_type: saved.contentType,
      download_url: `${API_V1_PREFIX}/files/${encodeURIComponent(saved.id)}`,
    });
    return true;
  } catch (error) {
    sendApiError(
      res,
      500,
      "upload_failed",
      error instanceof Error ? error.message : "Failed to store uploaded file.",
    );
    return true;
  }
}

async function handleApiFileDownloadRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  const match = url.pathname.match(/^\/api\/v1\/files\/([^/]+)$/);
  if (!match) {
    return false;
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    sendMethodNotAllowed(res, "GET, HEAD");
    return true;
  }
  const fileId = decodePathToken(match[1] ?? "");
  if (!fileId) {
    sendApiError(res, 400, "invalid_request", "Invalid file identifier.");
    return true;
  }
  const requestAuth = await authorizeGatewayHttpRequestOrReply({
    req,
    res,
    auth: opts.auth,
    trustedProxies: opts.trustedProxies,
    allowRealIpFallback: opts.allowRealIpFallback,
    rateLimiter: opts.rateLimiter,
  });
  if (!requestAuth) {
    return true;
  }
  try {
    const filePath = await resolveMediaBufferPath(fileId, FILES_SUBDIR);
    const stat = await fs.promises.stat(filePath);
    const filename = buildDownloadFilename(fileId);
    const contentType = await detectMime({ filePath });
    res.statusCode = 200;
    res.setHeader("Content-Length", String(stat.size));
    res.setHeader("Content-Type", contentType ?? "application/octet-stream");
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${filename.replace(/["\r\n]+/g, "_")}"`,
    );
    if (req.method === "HEAD") {
      res.end();
      return true;
    }
    const stream = fs.createReadStream(filePath);
    stream.on("error", (error) => {
      if (!res.headersSent) {
        sendApiError(
          res,
          500,
          "download_failed",
          error instanceof Error ? error.message : "Failed to read file.",
        );
        return;
      }
      res.destroy(error instanceof Error ? error : undefined);
    });
    stream.pipe(res);
    return true;
  } catch (error) {
    sendApiError(
      res,
      404,
      "not_found",
      error instanceof Error ? error.message : `File not found: ${fileId}`,
    );
    return true;
  }
}

export async function handleApiV1HttpRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: ApiV1HttpOptions,
): Promise<boolean> {
  const url = getRequestUrl(req);
  if (
    url.pathname !== `${API_V1_PREFIX}/health` &&
    !url.pathname.startsWith(`${API_V1_PREFIX}/tools/`) &&
    !url.pathname.startsWith(`${API_V1_PREFIX}/sessions/`) &&
    !url.pathname.startsWith(`${API_V1_PREFIX}/tasks/`) &&
    !url.pathname.startsWith(`${API_V1_PREFIX}/files/`)
  ) {
    return false;
  }

  return (
    (await handleApiHealthRequest(req, res)) ||
    (await handleApiToolsCatalogRequest(req, res, opts)) ||
    (await handleApiSessionCreateRequest(req, res, opts)) ||
    (await handleApiSessionDeleteRequest(req, res, opts)) ||
    (await handleApiTaskStatusRequest(req, res, opts)) ||
    (await handleApiFileUploadRequest(req, res, opts)) ||
    (await handleApiFileDownloadRequest(req, res, opts))
  );
}
