import { afterEach, describe, expect, it, vi } from "vitest";

type LoggerModule = typeof import("./logger.js");

const originalGetBuiltinModule = (
  process as NodeJS.Process & { getBuiltinModule?: (id: string) => unknown }
).getBuiltinModule;

async function importBrowserSafeLogger(params?: {
  resolvePreferredIronAgentTmpDir?: ReturnType<typeof vi.fn>;
}): Promise<{
  module: LoggerModule;
  resolvePreferredIronAgentTmpDir: ReturnType<typeof vi.fn>;
}> {
  vi.resetModules();
  const resolvePreferredIronAgentTmpDir =
    params?.resolvePreferredIronAgentTmpDir ??
    vi.fn(() => {
      throw new Error("resolvePreferredIronAgentTmpDir should not run during browser-safe import");
    });

  vi.doMock("../infra/tmp-ironagent-dir.js", async () => {
    const actual = await vi.importActual<typeof import("../infra/tmp-ironagent-dir.js")>(
      "../infra/tmp-ironagent-dir.js",
    );
    return {
      ...actual,
      resolvePreferredIronAgentTmpDir,
    };
  });

  Object.defineProperty(process, "getBuiltinModule", {
    configurable: true,
    value: undefined,
  });

  const module = await import("./logger.js");
  return { module, resolvePreferredIronAgentTmpDir };
}

describe("logging/logger browser-safe import", () => {
  afterEach(() => {
    vi.resetModules();
    vi.doUnmock("../infra/tmp-ironagent-dir.js");
    Object.defineProperty(process, "getBuiltinModule", {
      configurable: true,
      value: originalGetBuiltinModule,
    });
  });

  it("does not resolve the preferred temp dir at import time when node fs is unavailable", async () => {
    const { module, resolvePreferredIronAgentTmpDir } = await importBrowserSafeLogger();

    expect(resolvePreferredIronAgentTmpDir).not.toHaveBeenCalled();
    expect(module.DEFAULT_LOG_DIR).toBe("/tmp/ironagent");
    expect(module.DEFAULT_LOG_FILE).toBe("/tmp/ironagent/ironagent.log");
  });

  it("disables file logging when imported in a browser-like environment", async () => {
    const { module, resolvePreferredIronAgentTmpDir } = await importBrowserSafeLogger();

    expect(module.getResolvedLoggerSettings()).toMatchObject({
      level: "silent",
      file: "/tmp/ironagent/ironagent.log",
    });
    expect(module.isFileLogLevelEnabled("info")).toBe(false);
    expect(() => module.getLogger().info("browser-safe")).not.toThrow();
    expect(resolvePreferredIronAgentTmpDir).not.toHaveBeenCalled();
  });
});
