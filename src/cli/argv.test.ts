import { describe, expect, it } from "vitest";
import {
  buildParseArgv,
  getFlagValue,
  getCommandPath,
  getCommandPositionalsWithRootOptions,
  getCommandPathWithRootOptions,
  getPrimaryCommand,
  getPositiveIntFlagValue,
  getVerboseFlag,
  hasHelpOrVersion,
  hasFlag,
  isRootHelpInvocation,
  isRootVersionInvocation,
  shouldMigrateState,
  shouldMigrateStateFromPath,
} from "./argv.js";

describe("argv helpers", () => {
  it.each([
    {
      name: "help flag",
      argv: ["node", "ironagent", "--help"],
      expected: true,
    },
    {
      name: "version flag",
      argv: ["node", "ironagent", "-V"],
      expected: true,
    },
    {
      name: "normal command",
      argv: ["node", "ironagent", "status"],
      expected: false,
    },
    {
      name: "root -v alias",
      argv: ["node", "ironagent", "-v"],
      expected: true,
    },
    {
      name: "root -v alias with profile",
      argv: ["node", "ironagent", "--profile", "work", "-v"],
      expected: true,
    },
    {
      name: "root -v alias with log-level",
      argv: ["node", "ironagent", "--log-level", "debug", "-v"],
      expected: true,
    },
    {
      name: "subcommand -v should not be treated as version",
      argv: ["node", "ironagent", "acp", "-v"],
      expected: false,
    },
    {
      name: "root -v alias with equals profile",
      argv: ["node", "ironagent", "--profile=work", "-v"],
      expected: true,
    },
    {
      name: "subcommand path after global root flags should not be treated as version",
      argv: ["node", "ironagent", "--dev", "skills", "list", "-v"],
      expected: false,
    },
  ])("detects help/version flags: $name", ({ argv, expected }) => {
    expect(hasHelpOrVersion(argv)).toBe(expected);
  });

  it.each([
    {
      name: "root --version",
      argv: ["node", "ironagent", "--version"],
      expected: true,
    },
    {
      name: "root -V",
      argv: ["node", "ironagent", "-V"],
      expected: true,
    },
    {
      name: "root -v alias with profile",
      argv: ["node", "ironagent", "--profile", "work", "-v"],
      expected: true,
    },
    {
      name: "subcommand version flag",
      argv: ["node", "ironagent", "status", "--version"],
      expected: false,
    },
    {
      name: "unknown root flag with version",
      argv: ["node", "ironagent", "--unknown", "--version"],
      expected: false,
    },
  ])("detects root-only version invocations: $name", ({ argv, expected }) => {
    expect(isRootVersionInvocation(argv)).toBe(expected);
  });

  it.each([
    {
      name: "root --help",
      argv: ["node", "ironagent", "--help"],
      expected: true,
    },
    {
      name: "root -h",
      argv: ["node", "ironagent", "-h"],
      expected: true,
    },
    {
      name: "root --help with profile",
      argv: ["node", "ironagent", "--profile", "work", "--help"],
      expected: true,
    },
    {
      name: "subcommand --help",
      argv: ["node", "ironagent", "status", "--help"],
      expected: false,
    },
    {
      name: "help before subcommand token",
      argv: ["node", "ironagent", "--help", "status"],
      expected: false,
    },
    {
      name: "help after -- terminator",
      argv: ["node", "ironagent", "nodes", "invoke", "--", "device.status", "--help"],
      expected: false,
    },
    {
      name: "unknown root flag before help",
      argv: ["node", "ironagent", "--unknown", "--help"],
      expected: false,
    },
    {
      name: "unknown root flag after help",
      argv: ["node", "ironagent", "--help", "--unknown"],
      expected: false,
    },
  ])("detects root-only help invocations: $name", ({ argv, expected }) => {
    expect(isRootHelpInvocation(argv)).toBe(expected);
  });

  it.each([
    {
      name: "single command with trailing flag",
      argv: ["node", "ironagent", "status", "--json"],
      expected: ["status"],
    },
    {
      name: "two-part command",
      argv: ["node", "ironagent", "agents", "list"],
      expected: ["agents", "list"],
    },
    {
      name: "terminator cuts parsing",
      argv: ["node", "ironagent", "status", "--", "ignored"],
      expected: ["status"],
    },
  ])("extracts command path: $name", ({ argv, expected }) => {
    expect(getCommandPath(argv, 2)).toEqual(expected);
  });

  it("extracts command path while skipping known root option values", () => {
    expect(
      getCommandPathWithRootOptions(
        [
          "node",
          "ironagent",
          "--profile",
          "work",
          "--container",
          "demo",
          "--no-color",
          "config",
          "validate",
        ],
        2,
      ),
    ).toEqual(["config", "validate"]);
  });

  it("extracts routed config get positionals with interleaved root options", () => {
    expect(
      getCommandPositionalsWithRootOptions(
        ["node", "ironagent", "config", "get", "--log-level", "debug", "update.channel", "--json"],
        {
          commandPath: ["config", "get"],
          booleanFlags: ["--json"],
        },
      ),
    ).toEqual(["update.channel"]);
  });

  it("extracts routed config unset positionals with interleaved root options", () => {
    expect(
      getCommandPositionalsWithRootOptions(
        ["node", "ironagent", "config", "unset", "--profile", "work", "update.channel"],
        {
          commandPath: ["config", "unset"],
        },
      ),
    ).toEqual(["update.channel"]);
  });

  it("returns null when routed command sees unknown options", () => {
    expect(
      getCommandPositionalsWithRootOptions(
        ["node", "ironagent", "config", "get", "--mystery", "value", "update.channel"],
        {
          commandPath: ["config", "get"],
          booleanFlags: ["--json"],
        },
      ),
    ).toBeNull();
  });

  it.each([
    {
      name: "returns first command token",
      argv: ["node", "ironagent", "agents", "list"],
      expected: "agents",
    },
    {
      name: "returns null when no command exists",
      argv: ["node", "ironagent"],
      expected: null,
    },
    {
      name: "skips known root option values",
      argv: ["node", "ironagent", "--log-level", "debug", "status"],
      expected: "status",
    },
  ])("returns primary command: $name", ({ argv, expected }) => {
    expect(getPrimaryCommand(argv)).toBe(expected);
  });

  it.each([
    {
      name: "detects flag before terminator",
      argv: ["node", "ironagent", "status", "--json"],
      flag: "--json",
      expected: true,
    },
    {
      name: "ignores flag after terminator",
      argv: ["node", "ironagent", "--", "--json"],
      flag: "--json",
      expected: false,
    },
  ])("parses boolean flags: $name", ({ argv, flag, expected }) => {
    expect(hasFlag(argv, flag)).toBe(expected);
  });

  it.each([
    {
      name: "value in next token",
      argv: ["node", "ironagent", "status", "--timeout", "5000"],
      expected: "5000",
    },
    {
      name: "value in equals form",
      argv: ["node", "ironagent", "status", "--timeout=2500"],
      expected: "2500",
    },
    {
      name: "missing value",
      argv: ["node", "ironagent", "status", "--timeout"],
      expected: null,
    },
    {
      name: "next token is another flag",
      argv: ["node", "ironagent", "status", "--timeout", "--json"],
      expected: null,
    },
    {
      name: "flag appears after terminator",
      argv: ["node", "ironagent", "--", "--timeout=99"],
      expected: undefined,
    },
  ])("extracts flag values: $name", ({ argv, expected }) => {
    expect(getFlagValue(argv, "--timeout")).toBe(expected);
  });

  it("parses verbose flags", () => {
    expect(getVerboseFlag(["node", "ironagent", "status", "--verbose"])).toBe(true);
    expect(getVerboseFlag(["node", "ironagent", "status", "--debug"])).toBe(false);
    expect(getVerboseFlag(["node", "ironagent", "status", "--debug"], { includeDebug: true })).toBe(
      true,
    );
  });

  it.each([
    {
      name: "missing flag",
      argv: ["node", "ironagent", "status"],
      expected: undefined,
    },
    {
      name: "missing value",
      argv: ["node", "ironagent", "status", "--timeout"],
      expected: null,
    },
    {
      name: "valid positive integer",
      argv: ["node", "ironagent", "status", "--timeout", "5000"],
      expected: 5000,
    },
    {
      name: "invalid integer",
      argv: ["node", "ironagent", "status", "--timeout", "nope"],
      expected: undefined,
    },
  ])("parses positive integer flag values: $name", ({ argv, expected }) => {
    expect(getPositiveIntFlagValue(argv, "--timeout")).toBe(expected);
  });

  it.each([
    {
      name: "keeps plain node argv",
      rawArgs: ["node", "ironagent", "status"],
      expected: ["node", "ironagent", "status"],
    },
    {
      name: "keeps version-suffixed node binary",
      rawArgs: ["node-22", "ironagent", "status"],
      expected: ["node-22", "ironagent", "status"],
    },
    {
      name: "keeps windows versioned node exe",
      rawArgs: ["node-22.2.0.exe", "ironagent", "status"],
      expected: ["node-22.2.0.exe", "ironagent", "status"],
    },
    {
      name: "keeps dotted node binary",
      rawArgs: ["node-22.2", "ironagent", "status"],
      expected: ["node-22.2", "ironagent", "status"],
    },
    {
      name: "keeps dotted node exe",
      rawArgs: ["node-22.2.exe", "ironagent", "status"],
      expected: ["node-22.2.exe", "ironagent", "status"],
    },
    {
      name: "keeps absolute versioned node path",
      rawArgs: ["/usr/bin/node-22.2.0", "ironagent", "status"],
      expected: ["/usr/bin/node-22.2.0", "ironagent", "status"],
    },
    {
      name: "keeps node24 shorthand",
      rawArgs: ["node24", "ironagent", "status"],
      expected: ["node24", "ironagent", "status"],
    },
    {
      name: "keeps absolute node24 shorthand",
      rawArgs: ["/usr/bin/node24", "ironagent", "status"],
      expected: ["/usr/bin/node24", "ironagent", "status"],
    },
    {
      name: "keeps windows node24 exe",
      rawArgs: ["node24.exe", "ironagent", "status"],
      expected: ["node24.exe", "ironagent", "status"],
    },
    {
      name: "keeps nodejs binary",
      rawArgs: ["nodejs", "ironagent", "status"],
      expected: ["nodejs", "ironagent", "status"],
    },
    {
      name: "prefixes fallback when first arg is not a node launcher",
      rawArgs: ["node-dev", "ironagent", "status"],
      expected: ["node", "ironagent", "node-dev", "ironagent", "status"],
    },
    {
      name: "prefixes fallback when raw args start at program name",
      rawArgs: ["ironagent", "status"],
      expected: ["node", "ironagent", "status"],
    },
    {
      name: "keeps bun execution argv",
      rawArgs: ["bun", "src/entry.ts", "status"],
      expected: ["bun", "src/entry.ts", "status"],
    },
  ] as const)("builds parse argv from raw args: $name", ({ rawArgs, expected }) => {
    const parsed = buildParseArgv({
      programName: "ironagent",
      rawArgs: [...rawArgs],
    });
    expect(parsed).toEqual([...expected]);
  });

  it("builds parse argv from fallback args", () => {
    const fallbackArgv = buildParseArgv({
      programName: "ironagent",
      fallbackArgv: ["status"],
    });
    expect(fallbackArgv).toEqual(["node", "ironagent", "status"]);
  });

  it.each([
    { argv: ["node", "ironagent", "status"], expected: false },
    { argv: ["node", "ironagent", "health"], expected: false },
    { argv: ["node", "ironagent", "sessions"], expected: false },
    { argv: ["node", "ironagent", "config", "get", "update"], expected: false },
    { argv: ["node", "ironagent", "config", "unset", "update"], expected: false },
    { argv: ["node", "ironagent", "models", "list"], expected: false },
    { argv: ["node", "ironagent", "models", "status"], expected: false },
    { argv: ["node", "ironagent", "update", "status", "--json"], expected: false },
    { argv: ["node", "ironagent", "agent", "--message", "hi"], expected: false },
    { argv: ["node", "ironagent", "agents", "list"], expected: true },
    { argv: ["node", "ironagent", "message", "send"], expected: true },
  ] as const)("decides when to migrate state: $argv", ({ argv, expected }) => {
    expect(shouldMigrateState([...argv])).toBe(expected);
  });

  it.each([
    { path: ["status"], expected: false },
    { path: ["update", "status"], expected: false },
    { path: ["config", "get"], expected: false },
    { path: ["models", "status"], expected: false },
    { path: ["agents", "list"], expected: true },
  ])("reuses command path for migrate state decisions: $path", ({ path, expected }) => {
    expect(shouldMigrateStateFromPath(path)).toBe(expected);
  });
});
