import { afterEach, beforeEach, vi } from "vitest";
import { i18n } from "../../i18n/index.ts";
import { getSafeLocalStorage, getSafeSessionStorage } from "../../local-storage.ts";
import type { IronAgentApp } from "../app.ts";
import "../app.ts";
import { PRODUCT_ROOT_TAG } from "../branding.ts";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.OPEN;

  addEventListener() {}

  close() {
    this.readyState = MockWebSocket.CLOSED;
  }

  send() {}
}

export function mountApp(
  pathname: string,
  tagName: typeof PRODUCT_ROOT_TAG = PRODUCT_ROOT_TAG,
): IronAgentApp {
  window.history.replaceState({}, "", pathname);
  const app = document.createElement(tagName);
  document.body.append(app);
  app.connected = true;
  app.requestUpdate();
  return app;
}

export function registerAppMountHooks() {
  beforeEach(async () => {
    window.__IRONAGENT_CONTROL_UI_BASE_PATH__ = undefined;
    getSafeLocalStorage()?.clear();
    getSafeSessionStorage()?.clear();
    document.body.innerHTML = "";
    await i18n.setLocale("en");
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)) as unknown as typeof fetch,
    );
  });

  afterEach(async () => {
    window.__IRONAGENT_CONTROL_UI_BASE_PATH__ = undefined;
    getSafeLocalStorage()?.clear();
    getSafeSessionStorage()?.clear();
    document.body.innerHTML = "";
    await i18n.setLocale("en");
    vi.unstubAllGlobals();
  });
}
