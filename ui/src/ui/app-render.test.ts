import { describe, expect, it } from "vitest";
import { getVisibleLastError, shouldShowLoginGate } from "./app-render.ts";

describe("shouldShowLoginGate", () => {
  it("shows the login gate before any successful connection", () => {
    expect(
      shouldShowLoginGate({
        connected: false,
        hasConnectedOnce: false,
        reconnectingGateway: false,
      }),
    ).toBe(true);
  });

  it("keeps the current workspace visible during automatic reconnects", () => {
    expect(
      shouldShowLoginGate({
        connected: false,
        hasConnectedOnce: true,
        reconnectingGateway: true,
      }),
    ).toBe(false);
  });

  it("returns to the login gate when the disconnect is not a transient reconnect", () => {
    expect(
      shouldShowLoginGate({
        connected: false,
        hasConnectedOnce: true,
        reconnectingGateway: false,
      }),
    ).toBe(true);
  });

  it("hides transient reconnect errors from the top-level error surfaces", () => {
    expect(
      getVisibleLastError({
        lastError: "Restarting: config change",
        reconnectingGateway: true,
      }),
    ).toBeNull();
  });

  it("keeps genuine errors visible when not reconnecting", () => {
    expect(
      getVisibleLastError({
        lastError: "gateway token mismatch",
        reconnectingGateway: false,
      }),
    ).toBe("gateway token mismatch");
  });
});
