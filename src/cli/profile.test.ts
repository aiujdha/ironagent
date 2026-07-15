import path from "node:path";
import { describe, expect, it } from "vitest";
import { formatCliCommand } from "./command-format.js";
import { applyCliProfileEnv, parseCliProfileArgs } from "./profile.js";

describe("parseCliProfileArgs", () => {
  it("leaves gateway --dev for subcommands", () => {
    const res = parseCliProfileArgs([
      "node",
      "ironagent",
      "gateway",
      "--dev",
      "--allow-unconfigured",
    ]);
    if (!res.ok) {
      throw new Error(res.error);
    }
    expect(res.profile).toBeNull();
    expect(res.argv).toEqual(["node", "ironagent", "gateway", "--dev", "--allow-unconfigured"]);
  });

  it("leaves gateway --dev for subcommands after leading root options", () => {
    const res = parseCliProfileArgs([
      "node",
      "ironagent",
      "--no-color",
      "gateway",
      "--dev",
      "--allow-unconfigured",
    ]);
    if (!res.ok) {
      throw new Error(res.error);
    }
    expect(res.profile).toBeNull();
    expect(res.argv).toEqual([
      "node",
      "ironagent",
      "--no-color",
      "gateway",
      "--dev",
      "--allow-unconfigured",
    ]);
  });

  it("still accepts global --dev before subcommand", () => {
    const res = parseCliProfileArgs(["node", "ironagent", "--dev", "gateway"]);
    if (!res.ok) {
      throw new Error(res.error);
    }
    expect(res.profile).toBe("dev");
    expect(res.argv).toEqual(["node", "ironagent", "gateway"]);
  });

  it("parses --profile value and strips it", () => {
    const res = parseCliProfileArgs(["node", "ironagent", "--profile", "work", "status"]);
    if (!res.ok) {
      throw new Error(res.error);
    }
    expect(res.profile).toBe("work");
    expect(res.argv).toEqual(["node", "ironagent", "status"]);
  });

  it("parses interleaved --profile after the command token", () => {
    const res = parseCliProfileArgs(["node", "ironagent", "status", "--profile", "work", "--deep"]);
    if (!res.ok) {
      throw new Error(res.error);
    }
    expect(res.profile).toBe("work");
    expect(res.argv).toEqual(["node", "ironagent", "status", "--deep"]);
  });

  it("parses interleaved --dev after the command token", () => {
    const res = parseCliProfileArgs(["node", "ironagent", "status", "--dev"]);
    if (!res.ok) {
      throw new Error(res.error);
    }
    expect(res.profile).toBe("dev");
    expect(res.argv).toEqual(["node", "ironagent", "status"]);
  });

  it("rejects missing profile value", () => {
    const res = parseCliProfileArgs(["node", "ironagent", "--profile"]);
    expect(res.ok).toBe(false);
  });

  it.each([
    ["--dev first", ["node", "ironagent", "--dev", "--profile", "work", "status"]],
    ["--profile first", ["node", "ironagent", "--profile", "work", "--dev", "status"]],
    ["interleaved after command", ["node", "ironagent", "status", "--profile", "work", "--dev"]],
  ])("rejects combining --dev with --profile (%s)", (_name, argv) => {
    const res = parseCliProfileArgs(argv);
    expect(res.ok).toBe(false);
  });
});

describe("applyCliProfileEnv", () => {
  it("fills env defaults for dev profile", () => {
    const env: Record<string, string | undefined> = {};
    applyCliProfileEnv({
      profile: "dev",
      env,
      homedir: () => "/home/peter",
    });
    const expectedStateDir = path.join(path.resolve("/home/peter"), ".ironagent-dev");
    expect(env.IRONAGENT_PROFILE).toBe("dev");
    expect(env.IRONAGENT_STATE_DIR).toBe(expectedStateDir);
    expect(env.IRONAGENT_CONFIG_PATH).toBe(path.join(expectedStateDir, "ironagent.json"));
    expect(env.IRONAGENT_GATEWAY_PORT).toBe("19001");
  });

  it("does not override explicit env values", () => {
    const env: Record<string, string | undefined> = {
      IRONAGENT_STATE_DIR: "/custom",
      IRONAGENT_GATEWAY_PORT: "19099",
    };
    applyCliProfileEnv({
      profile: "dev",
      env,
      homedir: () => "/home/peter",
    });
    expect(env.IRONAGENT_STATE_DIR).toBe("/custom");
    expect(env.IRONAGENT_GATEWAY_PORT).toBe("19099");
    expect(env.IRONAGENT_CONFIG_PATH).toBe(path.join("/custom", "ironagent.json"));
  });

  it("uses IRONAGENT_HOME when deriving profile state dir", () => {
    const env: Record<string, string | undefined> = {
      IRONAGENT_HOME: "/srv/ironagent-home",
      HOME: "/home/other",
    };
    applyCliProfileEnv({
      profile: "work",
      env,
      homedir: () => "/home/fallback",
    });

    const resolvedHome = path.resolve("/srv/ironagent-home");
    expect(env.IRONAGENT_STATE_DIR).toBe(path.join(resolvedHome, ".ironagent-work"));
    expect(env.IRONAGENT_CONFIG_PATH).toBe(
      path.join(resolvedHome, ".ironagent-work", "ironagent.json"),
    );
  });
});

describe("formatCliCommand", () => {
  it.each([
    {
      name: "no profile is set",
      cmd: "ironagent doctor --fix",
      env: {},
      expected: "ironagent doctor --fix",
    },
    {
      name: "profile is default",
      cmd: "ironagent doctor --fix",
      env: { IRONAGENT_PROFILE: "default" },
      expected: "ironagent doctor --fix",
    },
    {
      name: "profile is Default (case-insensitive)",
      cmd: "ironagent doctor --fix",
      env: { IRONAGENT_PROFILE: "Default" },
      expected: "ironagent doctor --fix",
    },
    {
      name: "profile is invalid",
      cmd: "ironagent doctor --fix",
      env: { IRONAGENT_PROFILE: "bad profile" },
      expected: "ironagent doctor --fix",
    },
    {
      name: "--profile is already present",
      cmd: "ironagent --profile work doctor --fix",
      env: { IRONAGENT_PROFILE: "work" },
      expected: "ironagent --profile work doctor --fix",
    },
    {
      name: "--dev is already present",
      cmd: "ironagent --dev doctor",
      env: { IRONAGENT_PROFILE: "dev" },
      expected: "ironagent --dev doctor",
    },
  ])("returns command unchanged when $name", ({ cmd, env, expected }) => {
    expect(formatCliCommand(cmd, env)).toBe(expected);
  });

  it("inserts --profile flag when profile is set", () => {
    expect(formatCliCommand("ironagent doctor --fix", { IRONAGENT_PROFILE: "work" })).toBe(
      "ironagent --profile work doctor --fix",
    );
  });

  it("trims whitespace from profile", () => {
    expect(formatCliCommand("ironagent doctor --fix", { IRONAGENT_PROFILE: "  jbironagent  " })).toBe(
      "ironagent --profile jbironagent doctor --fix",
    );
  });

  it("handles command with no args after ironagent", () => {
    expect(formatCliCommand("ironagent", { IRONAGENT_PROFILE: "test" })).toBe(
      "ironagent --profile test",
    );
  });

  it("handles pnpm wrapper", () => {
    expect(formatCliCommand("pnpm ironagent doctor", { IRONAGENT_PROFILE: "work" })).toBe(
      "pnpm ironagent --profile work doctor",
    );
  });

  it("inserts --container when a container hint is set", () => {
    expect(
      formatCliCommand("ironagent gateway status --deep", { IRONAGENT_CONTAINER_HINT: "demo" }),
    ).toBe("ironagent --container demo gateway status --deep");
  });

  it("preserves both --container and --profile hints", () => {
    expect(
      formatCliCommand("ironagent doctor", {
        IRONAGENT_CONTAINER_HINT: "demo",
        IRONAGENT_PROFILE: "work",
      }),
    ).toBe("ironagent --container demo doctor");
  });

  it("does not prepend --container for update commands", () => {
    expect(formatCliCommand("ironagent update", { IRONAGENT_CONTAINER_HINT: "demo" })).toBe(
      "ironagent update",
    );
    expect(
      formatCliCommand("pnpm ironagent update --channel beta", { IRONAGENT_CONTAINER_HINT: "demo" }),
    ).toBe("pnpm ironagent update --channel beta");
  });
});
