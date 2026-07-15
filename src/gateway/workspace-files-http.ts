import { randomUUID } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import { basename } from "node:path";
import type { AuthRateLimiter } from "./auth-rate-limit.js";
import type { ResolvedGatewayAuth } from "./auth.js";
import {
  sendInvalidRequest,
  sendJson,
  sendMethodNotAllowed,
  sendUnauthorized,
} from "./http-common.js";
import { authorizeGatewayHttpRequestOrReply } from "./http-utils.js";
import { getWorkspaceFilesStorage } from "./workspace-files-storage.js";

const WORKSPACE_FILES_PATH = "/api/v1/workspace-files";
const WORKSPACE_FILES_UPLOAD_PATH = "/api/v1/workspace-files/upload";
const MAX_WORKSPACE_UPLOAD_BYTES = 25 * 1024 * 1024;
const WORKSPACE_UPLOAD_TIMEOUT_MS = 30_000;

function getPathname(req: IncomingMessage): string {
  const pathname = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`).pathname;
  return stripAssistantApiPrefix(pathname);
}

function stripAssistantApiPrefix(pathname: string): string {
  return pathname.replace(/^\/assistants\/[^/]+(?=\/api\/)/, "");
}

function readTextFormField(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function resolveUploadAuthHeader(req: IncomingMessage): string {
  const raw = req.headers["x-admin-token"];
  if (typeof raw === "string") {
    return raw.trim();
  }
  if (Array.isArray(raw)) {
    return (raw[0] ?? "").trim();
  }
  return "";
}

async function authorizeWorkspaceUploadRequest(params: {
  req: IncomingMessage;
  res: ServerResponse;
  auth: ResolvedGatewayAuth;
  trustedProxies?: string[];
  allowRealIpFallback?: boolean;
  rateLimiter?: AuthRateLimiter;
}): Promise<boolean> {
  if (params.auth.mode === "none") {
    return true;
  }
  const adminToken = resolveUploadAuthHeader(params.req);
  if (adminToken) {
    if (
      (params.auth.mode === "token" && adminToken === (params.auth.token ?? "")) ||
      (params.auth.mode === "password" && adminToken === (params.auth.password ?? ""))
    ) {
      return true;
    }
    sendUnauthorized(params.res);
    return false;
  }
  const requestAuth = await authorizeGatewayHttpRequestOrReply({
    req: params.req,
    res: params.res,
    auth: params.auth,
    trustedProxies: params.trustedProxies,
    allowRealIpFallback: params.allowRealIpFallback,
    rateLimiter: params.rateLimiter,
  });
  return Boolean(requestAuth);
}

function workspaceFileJsonHeaders(res: ServerResponse): void {
  res.setHeader("Cache-Control", "no-cache");
}

function buildDownloadFilename(value: string): string {
  return basename(value).replace(/"/g, "");
}

async function readRequestBodyBuffer(req: IncomingMessage): Promise<Buffer> {
  const contentLengthHeader = req.headers["content-length"];
  const declaredLength = Number.parseInt(
    Array.isArray(contentLengthHeader)
      ? (contentLengthHeader[0] ?? "")
      : (contentLengthHeader ?? ""),
    10,
  );
  if (Number.isFinite(declaredLength) && declaredLength > MAX_WORKSPACE_UPLOAD_BYTES) {
    throw new Error("payload too large");
  }

  return await new Promise<Buffer>((resolve, reject) => {
    let settled = false;
    let totalBytes = 0;
    const chunks: Buffer[] = [];
    const timer = setTimeout(
      () => finish(() => reject(new Error("request body timeout"))),
      WORKSPACE_UPLOAD_TIMEOUT_MS,
    );

    const cleanup = () => {
      clearTimeout(timer);
      req.removeListener("data", onData);
      req.removeListener("end", onEnd);
      req.removeListener("error", onError);
      req.removeListener("close", onClose);
    };

    const finish = (callback: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      callback();
    };

    const onData = (chunk: Buffer | string) => {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      totalBytes += buffer.length;
      if (totalBytes > MAX_WORKSPACE_UPLOAD_BYTES) {
        finish(() => reject(new Error("payload too large")));
        return;
      }
      chunks.push(buffer);
    };

    const onEnd = () => finish(() => resolve(Buffer.concat(chunks)));
    const onError = (error: Error) => finish(() => reject(error));
    const onClose = () => finish(() => reject(new Error("connection closed")));

    req.on("data", onData);
    req.on("end", onEnd);
    req.on("error", onError);
    req.on("close", onClose);
  });
}

export async function handleWorkspaceFilesHttpRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: {
    auth: ResolvedGatewayAuth;
    trustedProxies?: string[];
    allowRealIpFallback?: boolean;
    rateLimiter?: AuthRateLimiter;
  },
): Promise<boolean> {
  const pathname = getPathname(req);
  const storage = getWorkspaceFilesStorage();

  if (pathname === WORKSPACE_FILES_PATH) {
    if (req.method !== "GET") {
      sendMethodNotAllowed(res, "GET");
      return true;
    }
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
    const assistantId = url.searchParams.get("assistant_id")?.trim() ?? "";
    if (!assistantId) {
      sendInvalidRequest(res, "assistant_id is required");
      return true;
    }
    if (url.searchParams.get("sync") === "true") {
      storage.rebuildIndex(assistantId);
    }
    workspaceFileJsonHeaders(res);
    const files = storage.listFiles(assistantId);
    sendJson(res, 200, { files, total: files.length });
    return true;
  }

  if (pathname === WORKSPACE_FILES_UPLOAD_PATH) {
    if (req.method !== "POST") {
      sendMethodNotAllowed(res, "POST");
      return true;
    }
    const authorized = await authorizeWorkspaceUploadRequest({
      req,
      res,
      auth: opts.auth,
      trustedProxies: opts.trustedProxies,
      allowRealIpFallback: opts.allowRealIpFallback,
      rateLimiter: opts.rateLimiter,
    });
    if (!authorized) {
      return true;
    }
    let body: Buffer;
    try {
      body = await readRequestBodyBuffer(req);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("payload too large")) {
        sendJson(res, 413, { detail: "payload too large" });
        return true;
      }
      if (message.includes("timeout")) {
        sendJson(res, 408, { detail: "request body timeout" });
        return true;
      }
      sendInvalidRequest(res, "invalid multipart payload");
      return true;
    }
    let form: FormData;
    try {
      const contentTypeHeader = Array.isArray(req.headers["content-type"])
        ? req.headers["content-type"][0]
        : req.headers["content-type"];
      const request = new Response(new Uint8Array(body), {
        headers: contentTypeHeader ? { "content-type": contentTypeHeader } : {},
      });
      form = await request.formData();
    } catch {
      sendInvalidRequest(res, "invalid multipart payload");
      return true;
    }
    const file = form.get("file");
    if (!(file instanceof File)) {
      sendInvalidRequest(res, "file is required");
      return true;
    }
    const assistantId = readTextFormField(form, "assistant_id");
    const targetBucket =
      readTextFormField(form, "target_bucket") === "persistent" ? "persistent" : "temp";
    const sourceType = readTextFormField(form, "source_type") || "webui-chat";
    const sourceRef = readTextFormField(form, "source_ref") || "chat-send";
    if (!assistantId) {
      sendInvalidRequest(res, "assistant_id is required");
      return true;
    }
    const bytes = Buffer.from(await file.arrayBuffer());
    const created = await storage.createFile({
      assistantId,
      bucket: targetBucket,
      filename: file.name,
      contentType: file.type,
      sourceType,
      sourceRef,
      bytes,
    });
    workspaceFileJsonHeaders(res);
    sendJson(res, 200, {
      status: "accepted",
      file_id: created.file_id,
      assistant_id: created.assistant_id,
      filename: created.filename,
      relative_path: created.relative_path,
      bucket: created.bucket,
      message: "stored",
    });
    return true;
  }

  const fileMatch = pathname.match(/^\/api\/v1\/workspace-files\/([^/]+)$/);
  if (fileMatch) {
    if (req.method !== "DELETE") {
      sendMethodNotAllowed(res, "DELETE");
      return true;
    }
    const authorized = await authorizeWorkspaceUploadRequest({
      req,
      res,
      auth: opts.auth,
      trustedProxies: opts.trustedProxies,
      allowRealIpFallback: opts.allowRealIpFallback,
      rateLimiter: opts.rateLimiter,
    });
    if (!authorized) {
      return true;
    }
    const fileId = decodeURIComponent(fileMatch[1] ?? "");
    const existing = storage.getFile(fileId);
    if (!existing || existing.deleted) {
      sendJson(res, 404, { detail: "workspace file not found" });
      return true;
    }
    if (existing.bucket === "persistent") {
      workspaceFileJsonHeaders(res);
      sendJson(res, 202, {
        status: "pending_approval",
        file_id: existing.file_id,
        assistant_id: existing.assistant_id,
        relative_path: existing.relative_path,
        bucket: existing.bucket,
        approval_id: `workspace-delete-${randomUUID()}`,
        message: "persistent workspace file deletion is waiting for approval",
      });
      return true;
    }
    const deleted = await storage.deleteFile(fileId);
    if (!deleted) {
      sendJson(res, 404, { detail: "workspace file not found" });
      return true;
    }
    workspaceFileJsonHeaders(res);
    sendJson(res, 200, {
      status: "deleted",
      file_id: deleted.file_id,
      assistant_id: deleted.assistant_id,
      relative_path: deleted.relative_path,
      bucket: deleted.bucket,
    });
    return true;
  }

  const downloadMatch = pathname.match(/^\/api\/v1\/workspace-files\/([^/]+)\/download$/);
  if (!downloadMatch) {
    return false;
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    sendMethodNotAllowed(res, "GET, HEAD");
    return true;
  }
  const fileId = decodeURIComponent(downloadMatch[1] ?? "");
  const loaded = await storage.readFileBody(fileId);
  if (!loaded) {
    sendJson(res, 404, { detail: "workspace file not found" });
    return true;
  }
  const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
  const disposition = url.searchParams.get("disposition") === "inline" ? "inline" : "attachment";
  res.statusCode = 200;
  res.setHeader("Content-Type", loaded.contentType);
  res.setHeader(
    "Content-Disposition",
    `${disposition}; filename="${buildDownloadFilename(loaded.filename)}"`,
  );
  res.setHeader("Cache-Control", "no-cache");
  if (req.method === "HEAD") {
    res.end();
    return true;
  }
  res.end(loaded.body);
  return true;
}
