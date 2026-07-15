import { describe, expect, it } from "vitest";
import { shortenText } from "./text-format.js";

describe("shortenText", () => {
  it("returns original text when it fits", () => {
    expect(shortenText("ironagent", 16)).toBe("ironagent");
  });

  it("truncates and appends ellipsis when over limit", () => {
    expect(shortenText("ironagent-status-output", 10)).toBe("ironagent-…");
  });

  it("counts multi-byte characters correctly", () => {
    expect(shortenText("hello🙂world", 7)).toBe("hello🙂…");
  });
});
