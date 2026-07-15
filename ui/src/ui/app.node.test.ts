import { describe, expect, it } from "vitest";
import { PRODUCT_ROOT_TAG } from "./branding.ts";
import { mountApp, registerAppMountHooks } from "./test-helpers/app-mount.ts";
import "./app.ts";

registerAppMountHooks();

describe("app root element compatibility", () => {
  it("registers both root tags against the same app element class", () => {
    const modernAppElement = customElements.get(PRODUCT_ROOT_TAG);
    const legacyAppElement = customElements.get(PRODUCT_ROOT_TAG);

    expect(modernAppElement).toBeDefined();
    expect(legacyAppElement).toBeDefined();
    expect(legacyAppElement).not.toBe(modernAppElement);
    expect(Object.getPrototypeOf(legacyAppElement?.prototype)).toBe(modernAppElement?.prototype);
  });

  it("mounts the IronAgent root tag", async () => {
    const app = mountApp("/chat", PRODUCT_ROOT_TAG);
    await app.updateComplete;

    expect(app.tagName.toLowerCase()).toBe(PRODUCT_ROOT_TAG);
  });

  it("mounts the legacy IronAgent root tag", async () => {
    const app = mountApp("/chat", PRODUCT_ROOT_TAG);
    await app.updateComplete;

    expect(app.tagName.toLowerCase()).toBe(PRODUCT_ROOT_TAG);
  });
});
