import { describe, expect, it } from "vitest";
import { mountApp, registerAppMountHooks } from "./test-helpers/app-mount.ts";

registerAppMountHooks();

describe("topbar-only connection shell", () => {
  it("does not render the legacy sidebar version status", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    app.hello = {
      ok: true,
      server: { version: "1.2.3" },
    } as never;
    app.requestUpdate();
    await app.updateComplete;

    const version = app.querySelector<HTMLElement>(".sidebar-version");
    const statusDot = app.querySelector<HTMLElement>(".sidebar-version__status");
    expect(version).toBeNull();
    expect(statusDot).toBeNull();
    expect(app.querySelector(".topnav-brand__title")?.textContent).toContain("IronAgent");
  });
});
