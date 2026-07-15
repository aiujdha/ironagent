import { describe, expect, it } from "vitest";
import "../styles.css";
import { mountApp as mountTestApp, registerAppMountHooks } from "./test-helpers/app-mount.ts";

registerAppMountHooks();

function mountApp(pathname: string) {
  return mountTestApp(pathname);
}

function nextFrame() {
  return new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

function findConfirmButton(app: ReturnType<typeof mountApp>) {
  return Array.from(app.querySelectorAll<HTMLButtonElement>("button")).find(
    (button) => button.textContent?.trim() === "Confirm",
  );
}

async function confirmPendingGatewayChange(app: ReturnType<typeof mountApp>) {
  const confirmButton = findConfirmButton(app);
  expect(confirmButton).not.toBeUndefined();
  confirmButton?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  await app.updateComplete;
}

function expectConfirmedGatewayChange(app: ReturnType<typeof mountApp>) {
  expect(app.settings.gatewayUrl).toBe("wss://other-gateway.example/ironagent");
  expect(app.settings.token).toBe("abc123");
  expect(window.location.search).toBe("");
  expect(window.location.hash).toBe("");
}

describe("control UI routing", () => {
  it("hydrates the tab from the location", async () => {
    const app = mountApp("/sessions");
    await app.updateComplete;

    expect(app.tab).toBe("sessions");
    expect(window.location.pathname).toBe("/sessions");
  });

  it("respects /ui base paths", async () => {
    const app = mountApp("/ui/cron");
    await app.updateComplete;

    expect(app.basePath).toBe("/ui");
    expect(app.tab).toBe("cron");
    expect(window.location.pathname).toBe("/ui/cron");
  });

  it("infers nested base paths", async () => {
    const app = mountApp("/apps/ironagent/cron");
    await app.updateComplete;

    expect(app.basePath).toBe("/apps/ironagent");
    expect(app.tab).toBe("cron");
    expect(window.location.pathname).toBe("/apps/ironagent/cron");
  });

  it("honors explicit base path overrides", async () => {
    window.__IRONAGENT_CONTROL_UI_BASE_PATH__ = "/ironagent";
    const app = mountApp("/ironagent/sessions");
    await app.updateComplete;

    expect(app.basePath).toBe("/ironagent");
    expect(app.tab).toBe("sessions");
    expect(window.location.pathname).toBe("/ironagent/sessions");
  });

  it("updates the URL when selecting a tab", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    app.setTab("overview");
    await app.updateComplete;

    expect(app.tab).toBe("overview");
    expect(window.location.pathname).toBe("/overview");
  });

  it("renders the refreshed top navigation shell", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(app.querySelector(".topnav-shell")).not.toBeNull();
    expect(app.querySelector(".topnav-shell__content")).not.toBeNull();
    expect(app.querySelector(".topnav-shell__actions")).not.toBeNull();
    expect(app.querySelector(".topnav-shell .brand-title")).toBeNull();
  });

  it("renders the compact branded top navigation without the legacy sidebar shell", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(app.querySelector(".topnav-brand")).not.toBeNull();
    expect(app.querySelector(".topnav-brand__logo")).not.toBeNull();
    expect(app.querySelector(".topnav-brand__title")?.textContent).toContain("IronAgent");
    expect(app.querySelector(".sidebar-shell")).toBeNull();
    expect(app.querySelector(".sidebar-brand")).toBeNull();
    expect(app.querySelector(".shell-nav")).toBeNull();
  });

  it("does not render a desktop sidebar resizer or inject a custom nav width", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    app.applySettings({ ...app.settings, navWidth: 360 });
    await app.updateComplete;

    expect(app.querySelector(".sidebar-resizer")).toBeNull();
    const shell = app.querySelector<HTMLElement>(".shell");
    expect(shell?.style.getPropertyValue("--shell-nav-width")).toBe("");
  });

  it("hides section labels in collapsed mode", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    app.applySettings({ ...app.settings, navCollapsed: true });
    await app.updateComplete;

    expect(app.querySelector(".nav-section__label")).toBeNull();
    expect(app.querySelector(".sidebar-brand__logo")).toBeNull();
    expect(app.querySelector(".topnav-brand__logo")).not.toBeNull();
  });

  it("keeps topbar utilities available in collapsed mode", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    app.applySettings({ ...app.settings, navCollapsed: true });
    await app.updateComplete;

    expect(app.querySelector(".topnav-shell__actions")).not.toBeNull();
    expect(app.querySelector(".topbar-search")).not.toBeNull();
    expect(app.querySelector(".topbar-theme-mode")).not.toBeNull();
  });

  it("does not bring back the legacy desktop rail in collapsed mode", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    app.applySettings({ ...app.settings, navCollapsed: true });
    await app.updateComplete;

    expect(app.querySelector(".shell-nav")).toBeNull();
    expect(app.querySelector(".nav-item")).toBeNull();
    expect(app.querySelector(".content")).not.toBeNull();
  });

  it("keeps the active session when opening chat without sidebar navigation", async () => {
    const app = mountApp("/sessions?session=agent:main:subagent:task-123");
    await app.updateComplete;

    app.setTab("chat");
    await app.updateComplete;

    expect(app.tab).toBe("chat");
    expect(app.sessionKey).toBe("agent:main:subagent:task-123");
    expect(window.location.pathname).toBe("/chat");
    expect(window.location.search).toBe("?session=agent%3Amain%3Asubagent%3Atask-123");
  });

  it("keeps chat and nav usable on narrow viewports", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(window.matchMedia("(max-width: 768px)").matches).toBe(true);

    const split = app.querySelector(".chat-split-container");
    expect(split).not.toBeNull();
    if (split) {
      expect(getComputedStyle(split).position).not.toBe("fixed");
    }

    const chatMain = app.querySelector(".chat-main");
    expect(chatMain).not.toBeNull();
    if (chatMain) {
      expect(getComputedStyle(chatMain).display).not.toBe("none");
    }

    if (split) {
      split.classList.add("chat-split-container--open");
      await app.updateComplete;
      expect(getComputedStyle(split).position).toBe("fixed");
    }
    if (chatMain) {
      expect(getComputedStyle(chatMain).display).toBe("none");
    }
  });

  it("stacks the refreshed top navigation for narrow viewports", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(window.matchMedia("(max-width: 768px)").matches).toBe(true);

    const shell = app.querySelector<HTMLElement>(".topnav-shell");
    const content = app.querySelector<HTMLElement>(".topnav-shell__content");
    expect(shell).not.toBeNull();
    expect(content).not.toBeNull();
    if (!shell || !content) {
      return;
    }

    expect(getComputedStyle(shell).flexWrap).toBe("wrap");
    expect(getComputedStyle(content).width).not.toBe("auto");
  });

  it("keeps the mobile chat controls toggle visible beside the search row", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(window.matchMedia("(max-width: 768px)").matches).toBe(true);

    const shell = app.querySelector<HTMLElement>(".topnav-shell");
    const toggle = app.querySelector<HTMLElement>(".chat-controls-mobile-toggle");
    const actions = app.querySelector<HTMLElement>(".topnav-shell__actions");
    expect(shell).not.toBeNull();
    expect(toggle).not.toBeNull();
    expect(actions).not.toBeNull();
    if (!shell || !toggle || !actions) {
      return;
    }

    const shellWidth = parseFloat(getComputedStyle(shell).width);
    const toggleWidth = parseFloat(getComputedStyle(toggle).width);
    const actionsWidth = parseFloat(getComputedStyle(actions).width);

    expect(toggleWidth).toBeGreaterThan(0);
    expect(actionsWidth).toBeLessThan(shellWidth);
  });

  it("opens the mobile chat controls dropdown from the topbar toggle", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(window.matchMedia("(max-width: 768px)").matches).toBe(true);

    const toggle = app.querySelector<HTMLButtonElement>(".chat-controls-mobile-toggle");
    const dropdown = app.querySelector<HTMLElement>(".chat-controls-dropdown");
    expect(toggle).not.toBeNull();
    expect(dropdown).not.toBeNull();
    if (!toggle || !dropdown) {
      return;
    }

    expect(dropdown.classList.contains("open")).toBe(false);
    toggle.click();
    await app.updateComplete;

    expect(dropdown.classList.contains("open")).toBe(true);
    const styles = getComputedStyle(dropdown);
    expect(styles.position).toBe("absolute");
  });

  it("does not render a mobile sidenav drawer in the topbar-only layout", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    expect(window.matchMedia("(max-width: 768px)").matches).toBe(true);

    const shell = app.querySelector<HTMLElement>(".shell");
    expect(app.querySelector(".topbar-nav-toggle")).toBeNull();
    expect(app.querySelector(".shell-nav")).toBeNull();
    expect(shell?.classList.contains("shell--nav-drawer-open")).toBe(false);

    app.setTab("overview");
    await app.updateComplete;

    expect(app.tab).toBe("overview");
    expect(shell?.classList.contains("shell--nav-drawer-open")).toBe(false);
  });

  it("auto-scrolls chat history to the latest message", async () => {
    const app = mountApp("/chat");
    await app.updateComplete;

    const initialContainer: HTMLElement | null = app.querySelector(".chat-thread");
    expect(initialContainer).not.toBeNull();
    if (!initialContainer) {
      return;
    }
    initialContainer.style.maxHeight = "180px";
    initialContainer.style.overflow = "auto";

    app.chatMessages = Array.from({ length: 60 }, (_, index) => ({
      role: "assistant",
      content: `Line ${index} - ${"x".repeat(200)}`,
      timestamp: Date.now() + index,
    }));

    await app.updateComplete;
    for (let i = 0; i < 6; i++) {
      await nextFrame();
    }

    const container = app.querySelector(".chat-thread");
    expect(container).not.toBeNull();
    if (!container) {
      return;
    }
    const maxScroll = container.scrollHeight - container.clientHeight;
    expect(maxScroll).toBeGreaterThan(0);
    for (let i = 0; i < 10; i++) {
      if (container.scrollTop === maxScroll) {
        break;
      }
      await nextFrame();
    }
    expect(container.scrollTop).toBe(maxScroll);
  });

  it("hydrates token from query params and strips them", async () => {
    const app = mountApp("/ui/overview?token=abc123");
    await app.updateComplete;

    expect(app.settings.token).toBe("abc123");
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}").token).toBe(
      undefined,
    );
    expect(window.location.pathname).toBe("/ui/overview");
    expect(window.location.search).toBe("");
  });

  it("strips password URL params without importing them", async () => {
    const app = mountApp("/ui/overview?password=sekret");
    await app.updateComplete;

    expect(app.password).toBe("");
    expect(window.location.pathname).toBe("/ui/overview");
    expect(window.location.search).toBe("");
  });

  it("hydrates token from URL hash when settings already set", async () => {
    localStorage.setItem(
      "ironagent.control.settings.v1",
      JSON.stringify({ token: "existing-token", gatewayUrl: "wss://gateway.example/ironagent" }),
    );
    const app = mountApp("/ui/overview#token=abc123");
    await app.updateComplete;

    expect(app.settings.token).toBe("abc123");
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}")).toMatchObject({
      gatewayUrl: "wss://gateway.example/ironagent",
    });
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}").token).toBe(
      undefined,
    );
    expect(window.location.pathname).toBe("/ui/overview");
    expect(window.location.hash).toBe("");
  });

  it("hydrates token from URL hash and strips it", async () => {
    const app = mountApp("/ui/overview#token=abc123");
    await app.updateComplete;

    expect(app.settings.token).toBe("abc123");
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}").token).toBe(
      undefined,
    );
    expect(window.location.pathname).toBe("/ui/overview");
    expect(window.location.hash).toBe("");
  });

  it("clears the current token when the gateway URL changes", async () => {
    const app = mountApp("/ui/overview#token=abc123");
    await app.updateComplete;

    const gatewayUrlInput = app.querySelector<HTMLInputElement>(
      'input[placeholder="ws://100.x.y.z:18789"]',
    );
    expect(gatewayUrlInput).not.toBeNull();
    gatewayUrlInput!.value = "wss://other-gateway.example/ironagent";
    gatewayUrlInput!.dispatchEvent(new Event("input", { bubbles: true }));
    await app.updateComplete;

    expect(app.settings.gatewayUrl).toBe("wss://other-gateway.example/ironagent");
    expect(app.settings.token).toBe("");
  });

  it("keeps a hash token pending until the gateway URL change is confirmed", async () => {
    const app = mountApp(
      "/ui/overview?gatewayUrl=wss://other-gateway.example/ironagent#token=abc123",
    );
    await app.updateComplete;

    expect(app.settings.gatewayUrl).not.toBe("wss://other-gateway.example/ironagent");
    expect(app.settings.token).toBe("");

    await confirmPendingGatewayChange(app);

    expectConfirmedGatewayChange(app);
  });

  it("keeps a query token pending until the gateway URL change is confirmed", async () => {
    const app = mountApp(
      "/ui/overview?gatewayUrl=wss://other-gateway.example/ironagent&token=abc123",
    );
    await app.updateComplete;

    expect(app.settings.gatewayUrl).not.toBe("wss://other-gateway.example/ironagent");
    expect(app.settings.token).toBe("");

    await confirmPendingGatewayChange(app);

    expectConfirmedGatewayChange(app);
  });

  it("restores the token after a same-tab refresh", async () => {
    const first = mountApp("/ui/overview#token=abc123");
    await first.updateComplete;
    first.remove();

    const refreshed = mountApp("/ui/overview");
    await refreshed.updateComplete;

    expect(refreshed.settings.token).toBe("abc123");
    expect(JSON.parse(localStorage.getItem("ironagent.control.settings.v1") ?? "{}").token).toBe(
      undefined,
    );
  });
});
