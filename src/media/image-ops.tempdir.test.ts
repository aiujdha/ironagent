import fs from "node:fs/promises";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resolvePreferredIronAgentTmpDir } from "../infra/tmp-ironagent-dir.js";

describe("image-ops temp dir", () => {
  let createdTempDir = "";

  beforeEach(() => {
    process.env.IRONAGENT_IMAGE_BACKEND = "sips";
    const originalMkdtemp = fs.mkdtemp.bind(fs);
    vi.spyOn(fs, "mkdtemp").mockImplementation(async (prefix) => {
      createdTempDir = await originalMkdtemp(prefix);
      return createdTempDir;
    });
  });

  afterEach(() => {
    delete process.env.IRONAGENT_IMAGE_BACKEND;
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it("creates sips temp dirs under the secured IronAgent tmp root", async () => {
    const { getImageMetadata } = await import("./image-ops.js");
    const secureRoot = resolvePreferredIronAgentTmpDir();

    await getImageMetadata(Buffer.from("image"));

    expect(fs.mkdtemp).toHaveBeenCalledTimes(1);
    expect(fs.mkdtemp).toHaveBeenCalledWith(path.join(secureRoot, "ironagent-img-"));
    expect(createdTempDir.startsWith(path.join(secureRoot, "ironagent-img-"))).toBe(true);
    await expect(fs.access(createdTempDir)).rejects.toMatchObject({ code: "ENOENT" });
  });
});
