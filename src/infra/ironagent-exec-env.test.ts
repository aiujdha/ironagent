import { describe, expect, it } from "vitest";
import {
  ensureIronAgentExecMarkerOnProcess,
  markIronAgentExecEnv,
  IRONAGENT_CLI_ENV_VALUE,
  IRONAGENT_CLI_ENV_VAR,
} from "./ironagent-exec-env.js";

describe("markIronAgentExecEnv", () => {
  it("returns a cloned env object with the exec marker set", () => {
    const env = { PATH: "/usr/bin", IRONAGENT_CLI: "0" };
    const marked = markIronAgentExecEnv(env);

    expect(marked).toEqual({
      PATH: "/usr/bin",
      IRONAGENT_CLI: IRONAGENT_CLI_ENV_VALUE,
    });
    expect(marked).not.toBe(env);
    expect(env.IRONAGENT_CLI).toBe("0");
  });
});

describe("ensureIronAgentExecMarkerOnProcess", () => {
  it.each([
    {
      name: "mutates and returns the provided process env",
      env: { PATH: "/usr/bin" } as NodeJS.ProcessEnv,
    },
    {
      name: "overwrites an existing marker on the provided process env",
      env: { PATH: "/usr/bin", [IRONAGENT_CLI_ENV_VAR]: "0" } as NodeJS.ProcessEnv,
    },
  ])("$name", ({ env }) => {
    expect(ensureIronAgentExecMarkerOnProcess(env)).toBe(env);
    expect(env[IRONAGENT_CLI_ENV_VAR]).toBe(IRONAGENT_CLI_ENV_VALUE);
  });

  it("defaults to mutating process.env when no env object is provided", () => {
    const previous = process.env[IRONAGENT_CLI_ENV_VAR];
    delete process.env[IRONAGENT_CLI_ENV_VAR];

    try {
      expect(ensureIronAgentExecMarkerOnProcess()).toBe(process.env);
      expect(process.env[IRONAGENT_CLI_ENV_VAR]).toBe(IRONAGENT_CLI_ENV_VALUE);
    } finally {
      if (previous === undefined) {
        delete process.env[IRONAGENT_CLI_ENV_VAR];
      } else {
        process.env[IRONAGENT_CLI_ENV_VAR] = previous;
      }
    }
  });
});
