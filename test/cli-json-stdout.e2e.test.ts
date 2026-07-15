import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { withTempHome } from "./helpers/temp-home.ts";

describe("cli json stdout contract", () => {
  it("keeps `update status --json` stdout parseable even with legacy doctor preflight inputs", async () => {
    await withTempHome(
      async (tempHome) => {
        const legacyDir = path.join(tempHome, ".irondbot");
        await fs.mkdir(legacyDir, { recursive: true });
        await fs.writeFile(path.join(legacyDir, "irondbot.json"), "{}", "utf8");

        const env = {
          ...process.env,
          HOME: tempHome,
          USERPROFILE: tempHome,
          IRONAGENT_TEST_FAST: "1",
        };
        delete env.IRONAGENT_HOME;
        delete env.IRONAGENT_STATE_DIR;
        delete env.IRONAGENT_CONFIG_PATH;
        delete env.VITEST;

        const entry = path.resolve(process.cwd(), "ironagent.mjs");
        const result = spawnSync(
          process.execPath,
          [entry, "update", "status", "--json", "--timeout", "1"],
          { cwd: process.cwd(), env, encoding: "utf8" },
        );

        expect(result.status).toBe(0);
        const stdout = result.stdout.trim();
        expect(stdout.length).toBeGreaterThan(0);
        expect(() => JSON.parse(stdout)).not.toThrow();
        expect(stdout).not.toContain("Doctor warnings");
        expect(stdout).not.toContain("Doctor changes");
        expect(stdout).not.toContain("Config invalid");
      },
      { prefix: "ironagent-json-e2e-" },
    );
  });
});
