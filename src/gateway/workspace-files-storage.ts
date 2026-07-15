import { randomUUID } from "node:crypto";
import fs from "node:fs";
import fsPromises from "node:fs/promises";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { resolveAgentWorkspaceDir } from "../agents/agent-scope.js";
import { ensureAgentWorkspace } from "../agents/workspace.js";
import { loadConfig } from "../config/config.js";
import { resolveStateDir } from "../config/paths.js";
import { requireNodeSqlite } from "../infra/node-sqlite.js";
import { resolveUserPath } from "../utils.js";
import {
  createWorkspaceS3ObjectStore,
  type WorkspaceObjectStore,
  type WorkspaceObjectStoreConfig,
} from "./workspace-files-s3.runtime.js";

export type WorkspaceFileBucket = "temp" | "persistent";

export type WorkspaceFileRecord = {
  file_id: string;
  assistant_id: string;
  bucket: WorkspaceFileBucket;
  filename: string;
  relative_path: string;
  size_bytes: number;
  content_type: string;
  source_type: string;
  source_ref: string;
  updated_at: string;
  deleted: boolean;
};

type StoredWorkspaceFileRow = WorkspaceFileRecord & {
  cos_key: string | null;
  local_path: string;
};

type WorkspaceStorageResolvedConfig = {
  dbPath: string;
  objectStore: WorkspaceObjectStore | null;
  ironagentServiceUrl?: string;
  ironagentGatewayToken?: string;
  ironagentRuntimeId?: string;
};

type WorkspaceFileCreateParams = {
  assistantId: string;
  bucket: WorkspaceFileBucket;
  filename: string;
  contentType: string;
  sourceType: string;
  sourceRef: string;
  bytes: Buffer;
};

type WorkspaceFileReadResult = {
  record: WorkspaceFileRecord;
  body: Buffer;
  contentType: string;
  filename: string;
};

type WorkspaceStatements = {
  insertFile: ReturnType<DatabaseSync["prepare"]>;
  listFilesByAssistant: ReturnType<DatabaseSync["prepare"]>;
  selectFileById: ReturnType<DatabaseSync["prepare"]>;
  updateScannedFile: ReturnType<DatabaseSync["prepare"]>;
  markFileDeleted: ReturnType<DatabaseSync["prepare"]>;
};

const DEFAULT_CONTENT_TYPE = "application/octet-stream";
const SQLITE_BUSY_TIMEOUT_MS = 5000;
const WORKSPACE_DB_FILENAME = "workspace-files.sqlite";

let singletonStore: WorkspaceFilesStorage | null = null;

const workspaceStorageDeps = {
  now: () => new Date(),
  resolveRuntimeConfig: resolveWorkspaceFilesStorageConfig,
  createObjectStore: (config: WorkspaceObjectStoreConfig) => createWorkspaceS3ObjectStore(config),
};

function normalizeSlashPath(value: string): string {
  return value.replace(/\\/g, "/");
}

function sanitizeFilename(value: string): string {
  const base = path.basename(value).trim();
  const cleaned = base
    .replace(/[^\w.\- ]+/g, "-")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned || "file";
}

function buildRelativePath(bucket: WorkspaceFileBucket, originalName: string): string {
  const safeName = sanitizeFilename(originalName);
  const ext = path.extname(safeName);
  const stem = ext ? safeName.slice(0, -ext.length) : safeName;
  const suffix = randomUUID().slice(0, 8);
  const storedName = `${stem}-${suffix}${ext}`.replace(/^-+/, "");
  return normalizeSlashPath(path.posix.join(bucket, storedName));
}

function normalizeContentType(value: string): string {
  const trimmed = value.trim();
  return trimmed || DEFAULT_CONTENT_TYPE;
}

function workspaceDbPathFromEnv(env: NodeJS.ProcessEnv): string {
  const override = env.IRONAGENT_WORKSPACE_FILES_DB_PATH?.trim();
  if (override) {
    return resolveUserPath(override, env);
  }
  return path.join(resolveStateDir(env), WORKSPACE_DB_FILENAME);
}

function resolveWorkspaceFilesStorageConfig(
  env: NodeJS.ProcessEnv = process.env,
): WorkspaceStorageResolvedConfig {
  const endpoint = env.IRONAGENT_WORKSPACE_FILES_COS_ENDPOINT?.trim() ?? "";
  const region = env.IRONAGENT_WORKSPACE_FILES_COS_REGION?.trim() ?? "";
  const bucket = env.IRONAGENT_WORKSPACE_FILES_COS_BUCKET?.trim() ?? "";
  const accessKeyId = env.IRONAGENT_WORKSPACE_FILES_COS_ACCESS_KEY_ID?.trim() ?? "";
  const secretAccessKey = env.IRONAGENT_WORKSPACE_FILES_COS_SECRET_ACCESS_KEY?.trim() ?? "";
  const dbPath = workspaceDbPathFromEnv(env);
  if (!endpoint || !region || !bucket || !accessKeyId || !secretAccessKey) {
    return {
      dbPath,
      objectStore: null,
      ironagentServiceUrl: env.IRONAGENT_SERVICE_URL?.trim() || "",
      ironagentGatewayToken: env.IRONAGENT_GATEWAY_TOKEN?.trim() || "",
      ironagentRuntimeId: env.IRONAGENT_RUNTIME_ID?.trim() || "",
    };
  }
  return {
    dbPath,
    objectStore: workspaceStorageDeps.createObjectStore({
      endpoint,
      region,
      bucket,
      accessKeyId,
      secretAccessKey,
      forcePathStyle: env.IRONAGENT_WORKSPACE_FILES_COS_FORCE_PATH_STYLE !== "0",
    }),
    ironagentServiceUrl: env.IRONAGENT_SERVICE_URL?.trim() || "",
    ironagentGatewayToken: env.IRONAGENT_GATEWAY_TOKEN?.trim() || "",
    ironagentRuntimeId: env.IRONAGENT_RUNTIME_ID?.trim() || "",
  };
}

function ensureDbSchema(db: DatabaseSync): WorkspaceStatements {
  db.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA busy_timeout = ${SQLITE_BUSY_TIMEOUT_MS};
    CREATE TABLE IF NOT EXISTS workspace_files (
      file_id TEXT PRIMARY KEY,
      assistant_id TEXT NOT NULL,
      bucket TEXT NOT NULL,
      filename TEXT NOT NULL,
      relative_path TEXT NOT NULL,
      cos_key TEXT,
      local_path TEXT NOT NULL,
      size_bytes INTEGER NOT NULL,
      content_type TEXT NOT NULL,
      source_type TEXT NOT NULL,
      source_ref TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      deleted INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS workspace_files_assistant_idx
      ON workspace_files (assistant_id, updated_at DESC);
  `);
  return {
    insertFile: db.prepare(`
      INSERT INTO workspace_files (
        file_id,
        assistant_id,
        bucket,
        filename,
        relative_path,
        cos_key,
        local_path,
        size_bytes,
        content_type,
        source_type,
        source_ref,
        updated_at,
        deleted
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    `),
    listFilesByAssistant: db.prepare(`
      SELECT
        file_id,
        assistant_id,
        bucket,
        filename,
        relative_path,
        cos_key,
        local_path,
        size_bytes,
        content_type,
        source_type,
        source_ref,
        updated_at,
        deleted
      FROM workspace_files
      WHERE assistant_id = ? AND deleted = 0
      ORDER BY updated_at DESC, rowid DESC
    `),
    selectFileById: db.prepare(`
      SELECT
        file_id,
        assistant_id,
        bucket,
        filename,
        relative_path,
        cos_key,
        local_path,
        size_bytes,
        content_type,
        source_type,
        source_ref,
        updated_at,
        deleted
      FROM workspace_files
      WHERE file_id = ?
      LIMIT 1
    `),
    updateScannedFile: db.prepare(`
      UPDATE workspace_files
      SET
        local_path = ?,
        size_bytes = ?,
        source_type = ?,
        source_ref = ?,
        updated_at = ?,
        deleted = 0
      WHERE file_id = ?
    `),
    markFileDeleted: db.prepare(`
      UPDATE workspace_files
      SET deleted = 1, updated_at = ?
      WHERE file_id = ?
    `),
  };
}

function mapRow(row: Record<string, unknown>): StoredWorkspaceFileRow {
  const readString = (value: unknown, fallback = ""): string =>
    typeof value === "string" ? value : fallback;
  return {
    file_id: readString(row.file_id),
    assistant_id: readString(row.assistant_id),
    bucket: row.bucket === "persistent" ? "persistent" : "temp",
    filename: readString(row.filename),
    relative_path: readString(row.relative_path),
    cos_key: typeof row.cos_key === "string" ? row.cos_key : null,
    local_path: readString(row.local_path),
    size_bytes: Number(row.size_bytes ?? 0),
    content_type: readString(row.content_type, DEFAULT_CONTENT_TYPE),
    source_type: readString(row.source_type),
    source_ref: readString(row.source_ref),
    updated_at: readString(row.updated_at),
    deleted: Boolean(row.deleted),
  };
}

function stripPrivateFields(row: StoredWorkspaceFileRow): WorkspaceFileRecord {
  return {
    file_id: row.file_id,
    assistant_id: row.assistant_id,
    bucket: row.bucket,
    filename: row.filename,
    relative_path: row.relative_path,
    size_bytes: row.size_bytes,
    content_type: row.content_type,
    source_type: row.source_type,
    source_ref: row.source_ref,
    updated_at: row.updated_at,
    deleted: row.deleted,
  };
}

export class WorkspaceFilesStorage {
  private readonly db: DatabaseSync;
  private readonly statements: WorkspaceStatements;
  private readonly objectStore: WorkspaceObjectStore | null;
  private readonly ironagentServiceUrl: string;
  private readonly ironagentGatewayToken: string;
  private readonly ironagentRuntimeId: string;

  constructor(params: {
    dbPath: string;
    objectStore: WorkspaceObjectStore | null;
    ironagentServiceUrl?: string;
    ironagentGatewayToken?: string;
    ironagentRuntimeId?: string;
  }) {
    const { DatabaseSync } = requireNodeSqlite();
    fs.mkdirSync(path.dirname(params.dbPath), { recursive: true });
    this.db = new DatabaseSync(params.dbPath);
    this.objectStore = params.objectStore;
    this.ironagentServiceUrl = params.ironagentServiceUrl ?? "";
    this.ironagentGatewayToken = params.ironagentGatewayToken ?? "";
    this.ironagentRuntimeId = params.ironagentRuntimeId ?? "";
    this.statements = ensureDbSchema(this.db);
  }

  /** 异步上报文件变更事件到 IronAgent index-events 端点。
   *  不阻塞调用方。网络异常时重试一次，HTTP 4xx/5xx 只 warn。 */
  private async _reportIndexEvent(
    action: "created" | "deleted",
    record: WorkspaceFileRecord,
  ): Promise<void> {
    const baseUrl = this.ironagentServiceUrl;
    const token = this.ironagentGatewayToken;
    const runtimeId = this.ironagentRuntimeId;
    if (!baseUrl || !token || !runtimeId) {
      console.warn("[workspace-files] IronAgent env not configured, skipping index event");
      return;
    }

    // filename 必须使用 relative_path 的 basename，对齐 IronAgent 端校验
    const filename = record.relative_path.split("/").pop() || record.filename;
    const body = {
      assistant_id: record.assistant_id,
      runtime_id: runtimeId,
      events: [
        {
          event_id: randomUUID(),
          action,
          file_id: record.file_id,
          filename,
          relative_path: record.relative_path,
          bucket: record.bucket,
          size_bytes: record.size_bytes ?? 0,
          content_type: record.content_type || "",
          updated_at: record.updated_at,
        },
      ],
    };

    for (let attempt = 0; attempt <= 1; attempt++) {
      try {
        const response = await fetch(`${baseUrl}/api/v1/workspace-files/index-events`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify(body),
        });
        if (!response.ok) {
          console.warn(
            "[workspace-files] index event API returned",
            response.status,
            "for",
            action,
            record.file_id,
          );
        }
        return; // 网络成功（不论状态码）视为已投递
      } catch (err) {
        if (attempt === 0) {
          await new Promise((r) => setTimeout(r, 1500));
        } else {
          console.warn("[workspace-files] index event failed after retry (non-fatal):", err);
        }
      }
    }
  }

  async createFile(params: WorkspaceFileCreateParams): Promise<WorkspaceFileRecord> {
    const cfg = loadConfig();
    const assistantId = params.assistantId.trim();
    const workspaceDir = resolveAgentWorkspaceDir(cfg, assistantId);
    await ensureAgentWorkspace({ dir: workspaceDir });
    const relativePath = buildRelativePath(params.bucket, params.filename);
    const localPath = path.join(workspaceDir, relativePath);
    const cosKey = normalizeSlashPath(
      path.posix.join("assistants", assistantId, "workspace", relativePath),
    );
    const now = workspaceStorageDeps.now().toISOString();
    const fileId = randomUUID();
    await fsPromises.mkdir(path.dirname(localPath), { recursive: true });
    await fsPromises.writeFile(localPath, params.bytes);
    let remoteStored = false;
    try {
      if (this.objectStore) {
        await this.objectStore.putObject({
          key: cosKey,
          body: params.bytes,
          contentType: normalizeContentType(params.contentType),
        });
        remoteStored = true;
      }
      this.statements.insertFile.run(
        fileId,
        assistantId,
        params.bucket,
        sanitizeFilename(params.filename),
        relativePath,
        this.objectStore ? cosKey : null,
        localPath,
        params.bytes.byteLength,
        normalizeContentType(params.contentType),
        params.sourceType.trim(),
        params.sourceRef.trim(),
        now,
      );
      const record: WorkspaceFileRecord = {
        file_id: fileId,
        assistant_id: assistantId,
        bucket: params.bucket,
        filename: sanitizeFilename(params.filename),
        relative_path: relativePath,
        size_bytes: params.bytes.byteLength,
        content_type: normalizeContentType(params.contentType),
        source_type: params.sourceType.trim(),
        source_ref: params.sourceRef.trim(),
        updated_at: now,
        deleted: false,
      };
      // 异步上报 IronAgent index event（不阻塞，失败不抛出）
      this._reportIndexEvent("created", record).catch(() => {});
      return record;
    } catch (error) {
      await fsPromises.rm(localPath, { force: true });
      if (remoteStored && this.objectStore) {
        await this.objectStore.deleteObject({ key: cosKey }).catch(() => undefined);
      }
      throw error;
    }
  }

  /**
   * Rebuild the local SQLite index by scanning the workspace directory.
   *
   * This performs a synchronous filesystem walk and is intentionally
   * NOT called from listFiles().  Use it for background calibration
   * or one-off rebuilds — never in the user request path.
   */
  rebuildIndex(assistantId: string): void {
    const cfg = loadConfig();
    const workspaceDir = resolveAgentWorkspaceDir(cfg, assistantId);
    if (!fs.existsSync(workspaceDir)) {
      return;
    }
    const rows = this.statements.listFilesByAssistant.all(assistantId) as Array<
      Record<string, unknown>
    >;
    const existingByPath = new Map(rows.map((row) => [mapRow(row).relative_path, mapRow(row)]));
    const now = workspaceStorageDeps.now().toISOString();
    const walkFiles = (dir: string): string[] => {
      const files: string[] = [];
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const childPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          files.push(...walkFiles(childPath));
          continue;
        }
        if (entry.isFile()) {
          files.push(childPath);
        }
      }
      return files;
    };

    for (const bucket of ["temp", "persistent"] as const) {
      const bucketDir = path.join(workspaceDir, bucket);
      if (!fs.existsSync(bucketDir)) {
        continue;
      }
      for (const localPath of walkFiles(bucketDir)) {
        const relativeName = normalizeSlashPath(path.relative(bucketDir, localPath));
        if (!relativeName || relativeName.startsWith("..")) {
          continue;
        }
        const relativePath = normalizeSlashPath(path.posix.join(bucket, relativeName));
        const stat = fs.statSync(localPath);
        const existing = existingByPath.get(relativePath);
        if (existing) {
          if (
            existing.local_path === localPath &&
            existing.size_bytes === stat.size &&
            !existing.deleted
          ) {
            continue;
          }
          this.statements.updateScannedFile.run(
            localPath,
            stat.size,
            existing.source_type || "workspace-scan",
            existing.source_ref || "workspace-list",
            now,
            existing.file_id,
          );
          continue;
        }
        this.statements.insertFile.run(
          randomUUID(),
          assistantId,
          bucket,
          relativeName,
          relativePath,
          null,
          localPath,
          stat.size,
          DEFAULT_CONTENT_TYPE,
          "workspace-scan",
          "workspace-list",
          now,
        );
      }
    }
  }

  listFiles(assistantId: string): WorkspaceFileRecord[] {
    const rows = this.statements.listFilesByAssistant.all(assistantId) as Array<
      Record<string, unknown>
    >;
    return rows.map((row) => stripPrivateFields(mapRow(row)));
  }

  getFile(fileId: string): WorkspaceFileRecord | null {
    const row = this.statements.selectFileById.get(fileId) as Record<string, unknown> | undefined;
    return row ? stripPrivateFields(mapRow(row)) : null;
  }

  async readFileBody(fileId: string): Promise<WorkspaceFileReadResult | null> {
    const row = this.statements.selectFileById.get(fileId) as Record<string, unknown> | undefined;
    if (!row) {
      return null;
    }
    const mapped = mapRow(row);
    if (mapped.deleted) {
      return null;
    }
    try {
      const body = await fsPromises.readFile(mapped.local_path);
      return {
        record: stripPrivateFields(mapped),
        body,
        contentType: mapped.content_type,
        filename: mapped.filename,
      };
    } catch (error) {
      const code =
        typeof error === "object" && error !== null && "code" in error ? String(error.code) : "";
      if (code !== "ENOENT") {
        throw error;
      }
    }
    if (this.objectStore && mapped.cos_key) {
      const body = await this.objectStore.getObject({ key: mapped.cos_key });
      if (body) {
        await fsPromises.mkdir(path.dirname(mapped.local_path), { recursive: true });
        await fsPromises.writeFile(mapped.local_path, body);
        return {
          record: stripPrivateFields(mapped),
          body,
          contentType: mapped.content_type,
          filename: mapped.filename,
        };
      }
    }
    return null;
  }

  async deleteFile(fileId: string): Promise<WorkspaceFileRecord | null> {
    const row = this.statements.selectFileById.get(fileId) as Record<string, unknown> | undefined;
    if (!row) {
      return null;
    }
    const mapped = mapRow(row);
    if (!mapped.deleted) {
      if (this.objectStore && mapped.cos_key) {
        await this.objectStore.deleteObject({ key: mapped.cos_key });
      }
      await fsPromises.rm(mapped.local_path, { force: true });
      this.statements.markFileDeleted.run(workspaceStorageDeps.now().toISOString(), fileId);
    }
    const deletedRecord: WorkspaceFileRecord = {
      ...stripPrivateFields(mapped),
      updated_at: workspaceStorageDeps.now().toISOString(),
      deleted: true,
    };
    // 异步上报 IronAgent index event（不阻塞，失败不抛出）
    this._reportIndexEvent("deleted", deletedRecord).catch(() => {});
    return { ...stripPrivateFields(mapped), deleted: true };
  }

  close(): void {
    this.db.close();
  }
}

export function getWorkspaceFilesStorage(): WorkspaceFilesStorage {
  if (!singletonStore) {
    const config = workspaceStorageDeps.resolveRuntimeConfig(process.env);
    singletonStore = new WorkspaceFilesStorage(config);
  }
  return singletonStore;
}

export const __testing = {
  setDepsForTests(
    overrides: Partial<{
      now: typeof workspaceStorageDeps.now;
      resolveRuntimeConfig: typeof workspaceStorageDeps.resolveRuntimeConfig;
      createObjectStore: typeof workspaceStorageDeps.createObjectStore;
    }>,
  ) {
    Object.assign(workspaceStorageDeps, overrides);
  },
  resetDepsForTests() {
    workspaceStorageDeps.now = () => new Date();
    workspaceStorageDeps.resolveRuntimeConfig = resolveWorkspaceFilesStorageConfig;
    workspaceStorageDeps.createObjectStore = (config: WorkspaceObjectStoreConfig) =>
      createWorkspaceS3ObjectStore(config);
  },
  resetSingletonForTests() {
    singletonStore?.close();
    singletonStore = null;
  },
};
