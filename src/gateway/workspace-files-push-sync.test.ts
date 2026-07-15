import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GATEWAY_EVENT_WORKSPACE_FILES_CHANGED } from "./events.js";
import { startWorkspaceFilesPushSync } from "./workspace-files-push-sync.js";

type Handler = (path: string) => void;

class FakeWatcher {
  readonly handlers = new Map<string, Handler[]>();
  readonly close = vi.fn(async () => {});

  on(event: string, handler: Handler) {
    const entries = this.handlers.get(event) ?? [];
    entries.push(handler);
    this.handlers.set(event, entries);
    return this;
  }

  emit(event: string, changedPath: string) {
    for (const handler of this.handlers.get(event) ?? []) {
      handler(changedPath);
    }
  }
}

describe("workspace files push sync", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("rebuilds the workspace file index and broadcasts pushed change events", async () => {
    vi.useFakeTimers();
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "ironagent-workspace-push-"));
    const watchers: FakeWatcher[] = [];
    const watchFactory = vi.fn((targets: string | string[]) => {
      expect(targets).toEqual([`${root}/temp`, `${root}/persistent`]);
      const watcher = new FakeWatcher();
      watchers.push(watcher);
      return watcher as never;
    });
    const rebuildIndex = vi.fn();
    const broadcast = vi.fn();

    const handle = startWorkspaceFilesPushSync({
      cfg: {
        agents: {
          list: [{ id: "demo-main", workspace: root }],
        },
      },
      broadcast,
      debounceMs: 10,
      storage: { rebuildIndex },
      watchFactory: watchFactory as never,
    });

    expect(fs.existsSync(path.join(root, "temp"))).toBe(true);
    expect(fs.existsSync(path.join(root, "persistent"))).toBe(true);

    watchers[0]?.emit("add", path.join(root, "temp", "result.txt"));
    await vi.runOnlyPendingTimersAsync();

    expect(rebuildIndex).toHaveBeenCalledWith("demo-main");
    expect(broadcast).toHaveBeenCalledWith(
      GATEWAY_EVENT_WORKSPACE_FILES_CHANGED,
      expect.objectContaining({
        assistantId: "demo-main",
        changedPath: path.join(root, "temp", "result.txt"),
        source: "watch",
      }),
      { dropIfSlow: true },
    );

    await handle.stop();
    expect(watchers[0]?.close).toHaveBeenCalled();
    fs.rmSync(root, { recursive: true, force: true });
  });
});
