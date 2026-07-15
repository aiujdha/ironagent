import fs from "node:fs";
import path from "node:path";
import chokidar, { type FSWatcher } from "chokidar";
import { listAgentIds, resolveAgentWorkspaceDir } from "../agents/agent-scope.js";
import type { IronAgentConfig } from "../config/config.js";
import { createSubsystemLogger } from "../logging/subsystem.js";
import {
  GATEWAY_EVENT_WORKSPACE_FILES_CHANGED,
  type GatewayWorkspaceFilesChangedEventPayload,
} from "./events.js";
import type { GatewayBroadcastFn } from "./server-broadcast.js";
import { getWorkspaceFilesStorage, type WorkspaceFilesStorage } from "./workspace-files-storage.js";

type ChokidarWatch = typeof chokidar.watch;

type WorkspaceFilesPushSyncHandle = {
  stop: () => Promise<void>;
};

const DEFAULT_WORKSPACE_FILES_PUSH_SYNC_DEBOUNCE_MS = 300;
const log = createSubsystemLogger("gateway/workspace-files");

function toWatchGlobRoot(raw: string): string {
  return raw.replaceAll("\\", "/").replace(/\/+$/, "");
}

function resolveWatchTargets(
  cfg: IronAgentConfig,
): Array<{ assistantId: string; targets: string[] }> {
  const targets: Array<{ assistantId: string; targets: string[] }> = [];
  for (const assistantId of listAgentIds(cfg)) {
    try {
      const workspaceDir = resolveAgentWorkspaceDir(cfg, assistantId);
      const buckets = ["temp", "persistent"].map((bucket) => {
        const bucketDir = path.join(workspaceDir, bucket);
        fs.mkdirSync(bucketDir, { recursive: true });
        return toWatchGlobRoot(bucketDir);
      });
      targets.push({ assistantId, targets: buckets });
    } catch (err) {
      log.warn(`workspace files watcher skipped (${assistantId}): ${String(err)}`);
    }
  }
  return targets;
}

export function startWorkspaceFilesPushSync(params: {
  cfg: IronAgentConfig;
  broadcast: GatewayBroadcastFn;
  debounceMs?: number;
  storage?: Pick<WorkspaceFilesStorage, "rebuildIndex">;
  watchFactory?: ChokidarWatch;
}): WorkspaceFilesPushSyncHandle {
  const debounceMs =
    typeof params.debounceMs === "number" && Number.isFinite(params.debounceMs)
      ? Math.max(0, params.debounceMs)
      : DEFAULT_WORKSPACE_FILES_PUSH_SYNC_DEBOUNCE_MS;
  const storage = params.storage ?? getWorkspaceFilesStorage();
  const watchFactory = params.watchFactory ?? chokidar.watch.bind(chokidar);
  const watchers: FSWatcher[] = [];
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  const pendingPaths = new Map<string, string | undefined>();

  const emitChange = (assistantId: string, changedPath?: string) => {
    try {
      storage.rebuildIndex(assistantId);
      const payload: GatewayWorkspaceFilesChangedEventPayload = {
        assistantId,
        changedPath,
        source: "watch",
        ts: Date.now(),
      };
      params.broadcast(GATEWAY_EVENT_WORKSPACE_FILES_CHANGED, payload, { dropIfSlow: true });
    } catch (err) {
      log.warn(`workspace files sync failed (${assistantId}): ${String(err)}`);
    }
  };

  const schedule = (assistantId: string, changedPath?: string) => {
    pendingPaths.set(assistantId, changedPath ?? pendingPaths.get(assistantId));
    const existing = timers.get(assistantId);
    if (existing) {
      clearTimeout(existing);
    }
    timers.set(
      assistantId,
      setTimeout(() => {
        timers.delete(assistantId);
        const pendingPath = pendingPaths.get(assistantId);
        pendingPaths.delete(assistantId);
        emitChange(assistantId, pendingPath);
      }, debounceMs),
    );
  };

  for (const { assistantId, targets } of resolveWatchTargets(params.cfg)) {
    const watcher = watchFactory(targets, {
      ignoreInitial: true,
      awaitWriteFinish: {
        stabilityThreshold: debounceMs,
        pollInterval: 100,
      },
      usePolling: process.env.IRON_AGENT_WORKSPACE_FILES_WATCH_USE_POLLING !== "0",
      interval: 500,
      binaryInterval: 1000,
      ignored: [
        /(^|[\\/])\.git([\\/]|$)/,
        /(^|[\\/])node_modules([\\/]|$)/,
        /(^|[\\/])__pycache__([\\/]|$)/,
      ],
    });
    watcher.on("add", (p) => schedule(assistantId, p));
    watcher.on("change", (p) => schedule(assistantId, p));
    watcher.on("unlink", (p) => schedule(assistantId, p));
    watcher.on("error", (err) => {
      log.warn(`workspace files watcher error (${assistantId}): ${String(err)}`);
    });
    watchers.push(watcher);
  }

  return {
    stop: async () => {
      for (const timer of timers.values()) {
        clearTimeout(timer);
      }
      timers.clear();
      pendingPaths.clear();
      await Promise.all(
        watchers.map(async (watcher) => {
          try {
            await watcher.close();
          } catch {
            // Best-effort shutdown.
          }
        }),
      );
    },
  };
}
