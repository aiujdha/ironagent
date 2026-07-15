import { describe, expect, it } from "vitest";
import { normalizePackageTagInput } from "./package-tag.js";

describe("normalizePackageTagInput", () => {
  const packageNames = ["ironagent", "@ironagent/plugin"] as const;

  it.each([
    { input: undefined, expected: null },
    { input: "   ", expected: null },
    { input: "ironagent@beta", expected: "beta" },
    { input: "@ironagent/plugin@2026.2.24", expected: "2026.2.24" },
    { input: "ironagent@   ", expected: null },
    { input: "ironagent", expected: null },
    { input: " @ironagent/plugin ", expected: null },
    { input: " latest ", expected: "latest" },
    { input: "@other/plugin@beta", expected: "@other/plugin@beta" },
    { input: "ironagenter@beta", expected: "ironagenter@beta" },
  ] satisfies ReadonlyArray<{ input: string | undefined; expected: string | null }>)(
    "normalizes %j",
    ({ input, expected }) => {
      expect(normalizePackageTagInput(input, packageNames)).toBe(expected);
    },
  );
});
