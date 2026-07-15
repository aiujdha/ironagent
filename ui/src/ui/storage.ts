const SETTINGS_KEY_PREFIX = "ironagent.control.settings.v1:";
const SETTINGS_KEY = "ironagent.control.settings.v1";
const LEGACY_SETTINGS_KEY_PREFIX = "ironagent.control.settings.v1:";
const LEGACY_SETTINGS_KEY = "ironagent.control.settings.v1";
const TOKEN_SESSION_KEY_PREFIX = "ironagent.control.token.v1:";
const LEGACY_TOKEN_SESSION_KEY_PREFIX = "ironagent.control.token.v1:";
const LEGACY_TOKEN_SESSION_KEY = "ironagent.control.token.v1";
const MAX_SCOPED_SESSION_ENTRIES = 10;

function settingsKeyForGateway(gatewayUrl: string): string {
  return `${SETTINGS_KEY_PREFIX}${normalizeGatewayTokenScope(gatewayUrl)}`;
}

function legacySettingsKeyForGateway(gatewayUrl: string): string {
  return `${LEGACY_SETTINGS_KEY_PREFIX}${normalizeGatewayTokenScope(gatewayUrl)}`;
}

type ScopedSessionSelection = {
  sessionKey: string;
  lastActiveSessionKey: string;
};

type PersistedUiSettings = Omit<UiSettings, "token" | "sessionKey" | "lastActiveSessionKey"> & {
  token?: never;
  sessionKey?: string;
  lastActiveSessionKey?: string;
  sessionsByGateway?: Record<string, ScopedSessionSelection>;
};

import { isSupportedLocale } from "../i18n/index.ts";
import { getSafeLocalStorage, getSafeSessionStorage } from "../local-storage.ts";
import { parseAssistantSingleEntryRoute } from "./assistant-route.ts";
import { readCompatStorageValue, writeCompatStorageValue } from "./brand-storage.ts";
import { inferBasePathFromPathname, normalizeBasePath } from "./navigation.ts";
import { parseThemeSelection, type ThemeMode, type ThemeName } from "./theme.ts";

export const BORDER_RADIUS_STOPS = [0, 25, 50, 75, 100] as const;
export type BorderRadiusStop = (typeof BORDER_RADIUS_STOPS)[number];

function snapBorderRadius(value: number): BorderRadiusStop {
  let best: BorderRadiusStop = BORDER_RADIUS_STOPS[0];
  let bestDist = Math.abs(value - best);
  for (const stop of BORDER_RADIUS_STOPS) {
    const dist = Math.abs(value - stop);
    if (dist < bestDist) {
      best = stop;
      bestDist = dist;
    }
  }
  return best;
}

export type UiSettings = {
  gatewayUrl: string;
  token: string;
  sessionKey: string;
  lastActiveSessionKey: string;
  theme: ThemeName;
  themeMode: ThemeMode;
  chatFocusMode: boolean;
  chatShowThinking: boolean;
  chatShowToolCalls: boolean;
  splitRatio: number; // Sidebar split ratio (0.4 to 0.7, default 0.6)
  navCollapsed: boolean; // Collapsible sidebar state
  navWidth: number; // Sidebar width when expanded (240–400px)
  navGroupsCollapsed: Record<string, boolean>; // Which nav groups are collapsed
  borderRadius: number; // Corner roundness (0–100, default 50)
  locale?: string;
};

function isViteDevPage(): boolean {
  if (typeof document === "undefined") {
    return false;
  }
  return Boolean(document.querySelector('script[src*="/@vite/client"]'));
}

function formatHostWithPort(hostname: string, port: string): string {
  const normalizedHost = hostname.includes(":") ? `[${hostname}]` : hostname;
  return `${normalizedHost}:${port}`;
}

function deriveDefaultGatewayUrl(): { pageUrl: string; effectiveUrl: string } {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const configured =
    typeof window !== "undefined" &&
    typeof window.__IRONAGENT_CONTROL_UI_BASE_PATH__ === "string" &&
    window.__IRONAGENT_CONTROL_UI_BASE_PATH__.trim();
  const basePath = configured
    ? normalizeBasePath(configured)
    : inferBasePathFromPathname(location.pathname);
  const singleEntryRoute = parseAssistantSingleEntryRoute(location.pathname);
  const pageUrl = `${proto}://${location.host}${basePath}`;
  const gatewayPath = singleEntryRoute?.wsBasePath ?? basePath;
  const routeGatewayUrl = `${proto}://${location.host}${gatewayPath}`;
  if (!isViteDevPage()) {
    return { pageUrl, effectiveUrl: routeGatewayUrl };
  }
  const effectiveUrl = `${proto}://${formatHostWithPort(location.hostname, "18789")}`;
  return { pageUrl, effectiveUrl };
}

function getSessionStorage(): Storage | null {
  return getSafeSessionStorage();
}

function normalizeGatewayTokenScope(gatewayUrl: string): string {
  const trimmed = gatewayUrl.trim();
  if (!trimmed) {
    return "default";
  }
  try {
    const base =
      typeof location !== "undefined"
        ? `${location.protocol}//${location.host}${location.pathname || "/"}`
        : undefined;
    const parsed = base ? new URL(trimmed, base) : new URL(trimmed);
    const pathname =
      parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/+$/, "") || parsed.pathname;
    return `${parsed.protocol}//${parsed.host}${pathname}`;
  } catch {
    return trimmed;
  }
}

function tokenSessionKeyForGateway(gatewayUrl: string): string {
  return `${TOKEN_SESSION_KEY_PREFIX}${normalizeGatewayTokenScope(gatewayUrl)}`;
}

function legacyTokenSessionKeyForGateway(gatewayUrl: string): string {
  return `${LEGACY_TOKEN_SESSION_KEY_PREFIX}${normalizeGatewayTokenScope(gatewayUrl)}`;
}

function readPersistedSettingsRecord(gatewayUrl: string): {
  raw: string;
  migrated: boolean;
} | null {
  const storage = getSafeLocalStorage();
  if (!storage) {
    return null;
  }

  const currentScopedKey = settingsKeyForGateway(gatewayUrl);
  const currentValue =
    storage.getItem(currentScopedKey) ??
    storage.getItem(SETTINGS_KEY) ??
    storage.getItem(`${SETTINGS_KEY_PREFIX}default`);
  if (currentValue) {
    return { raw: currentValue, migrated: false };
  }

  const legacyValue =
    storage.getItem(legacySettingsKeyForGateway(gatewayUrl)) ??
    storage.getItem(`${LEGACY_SETTINGS_KEY_PREFIX}default`) ??
    storage.getItem(LEGACY_SETTINGS_KEY);
  if (!legacyValue) {
    return null;
  }

  return { raw: legacyValue, migrated: true };
}

function resolveScopedSessionSelection(
  gatewayUrl: string,
  parsed: PersistedUiSettings,
  defaults: UiSettings,
): ScopedSessionSelection {
  const scope = normalizeGatewayTokenScope(gatewayUrl);
  const scoped = parsed.sessionsByGateway?.[scope];
  if (
    scoped &&
    typeof scoped.sessionKey === "string" &&
    scoped.sessionKey.trim() &&
    typeof scoped.lastActiveSessionKey === "string" &&
    scoped.lastActiveSessionKey.trim()
  ) {
    return {
      sessionKey: scoped.sessionKey.trim(),
      lastActiveSessionKey: scoped.lastActiveSessionKey.trim(),
    };
  }

  const legacySessionKey =
    typeof parsed.sessionKey === "string" && parsed.sessionKey.trim()
      ? parsed.sessionKey.trim()
      : defaults.sessionKey;
  const legacyLastActiveSessionKey =
    typeof parsed.lastActiveSessionKey === "string" && parsed.lastActiveSessionKey.trim()
      ? parsed.lastActiveSessionKey.trim()
      : legacySessionKey || defaults.lastActiveSessionKey;

  return {
    sessionKey: legacySessionKey,
    lastActiveSessionKey: legacyLastActiveSessionKey,
  };
}

function loadSessionToken(gatewayUrl: string): string {
  try {
    const storage = getSessionStorage();
    if (!storage) {
      return "";
    }
    const token =
      readCompatStorageValue(
        "session",
        {
          current: tokenSessionKeyForGateway(gatewayUrl),
          legacy: [legacyTokenSessionKeyForGateway(gatewayUrl), LEGACY_TOKEN_SESSION_KEY],
        },
        { migrateLegacy: true },
      ) ?? "";
    storage.removeItem(LEGACY_TOKEN_SESSION_KEY);
    return token.trim();
  } catch {
    return "";
  }
}

function persistSessionToken(gatewayUrl: string, token: string) {
  try {
    const storage = getSessionStorage();
    if (!storage) {
      return;
    }
    const normalized = token.trim();
    if (normalized) {
      writeCompatStorageValue(
        "session",
        {
          current: tokenSessionKeyForGateway(gatewayUrl),
          legacy: legacyTokenSessionKeyForGateway(gatewayUrl),
        },
        normalized,
      );
      storage.removeItem(LEGACY_TOKEN_SESSION_KEY);
      return;
    }
    storage.removeItem(tokenSessionKeyForGateway(gatewayUrl));
    storage.removeItem(legacyTokenSessionKeyForGateway(gatewayUrl));
    storage.removeItem(LEGACY_TOKEN_SESSION_KEY);
  } catch {
    // best-effort
  }
}

export function loadSettings(): UiSettings {
  const { pageUrl: pageDerivedUrl, effectiveUrl: defaultUrl } = deriveDefaultGatewayUrl();

  const defaults: UiSettings = {
    gatewayUrl: defaultUrl,
    token: loadSessionToken(defaultUrl),
    sessionKey: "main",
    lastActiveSessionKey: "main",
    theme: "iron",
    themeMode: "system",
    locale: "zh-CN",
    chatFocusMode: false,
    chatShowThinking: true,
    chatShowToolCalls: true,
    splitRatio: 0.6,
    navCollapsed: false,
    navWidth: 220,
    navGroupsCollapsed: {},
    borderRadius: 50,
  };

  try {
    const persistedRecord = readPersistedSettingsRecord(defaults.gatewayUrl);
    if (!persistedRecord?.raw) {
      return defaults;
    }
    const parsed = JSON.parse(persistedRecord.raw) as PersistedUiSettings;
    const parsedGatewayUrl =
      typeof parsed.gatewayUrl === "string" && parsed.gatewayUrl.trim()
        ? parsed.gatewayUrl.trim()
        : defaults.gatewayUrl;
    const gatewayUrl =
      parsedGatewayUrl === pageDerivedUrl || shouldIgnorePersistedGatewayUrl(parsedGatewayUrl)
        ? defaultUrl
        : parsedGatewayUrl;
    const scopedSessionSelection = resolveScopedSessionSelection(gatewayUrl, parsed, defaults);
    const { theme, mode } = parseThemeSelection(
      (parsed as { theme?: unknown }).theme,
      (parsed as { themeMode?: unknown }).themeMode,
    );
    const settings = {
      gatewayUrl,
      // Gateway auth is intentionally in-memory only; scrub any legacy persisted token on load.
      token: loadSessionToken(gatewayUrl),
      sessionKey: scopedSessionSelection.sessionKey,
      lastActiveSessionKey: scopedSessionSelection.lastActiveSessionKey,
      theme,
      themeMode: mode,
      chatFocusMode:
        typeof parsed.chatFocusMode === "boolean" ? parsed.chatFocusMode : defaults.chatFocusMode,
      chatShowThinking:
        typeof parsed.chatShowThinking === "boolean"
          ? parsed.chatShowThinking
          : defaults.chatShowThinking,
      chatShowToolCalls:
        typeof parsed.chatShowToolCalls === "boolean"
          ? parsed.chatShowToolCalls
          : defaults.chatShowToolCalls,
      splitRatio:
        typeof parsed.splitRatio === "number" &&
        parsed.splitRatio >= 0.4 &&
        parsed.splitRatio <= 0.7
          ? parsed.splitRatio
          : defaults.splitRatio,
      navCollapsed:
        typeof parsed.navCollapsed === "boolean" ? parsed.navCollapsed : defaults.navCollapsed,
      navWidth:
        typeof parsed.navWidth === "number" && parsed.navWidth >= 200 && parsed.navWidth <= 400
          ? parsed.navWidth
          : defaults.navWidth,
      navGroupsCollapsed:
        typeof parsed.navGroupsCollapsed === "object" && parsed.navGroupsCollapsed !== null
          ? parsed.navGroupsCollapsed
          : defaults.navGroupsCollapsed,
      borderRadius:
        typeof parsed.borderRadius === "number" &&
        parsed.borderRadius >= 0 &&
        parsed.borderRadius <= 100
          ? snapBorderRadius(parsed.borderRadius)
          : defaults.borderRadius,
      locale: isSupportedLocale(parsed.locale) ? parsed.locale : defaults.locale,
    };
    if (persistedRecord.migrated || "token" in parsed) {
      persistSettings(settings);
    }
    return settings;
  } catch {
    return defaults;
  }
}

function shouldIgnorePersistedGatewayUrl(gatewayUrl: string): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  const singleEntryRoute = parseAssistantSingleEntryRoute(window.location.pathname);
  if (!singleEntryRoute) {
    return false;
  }
  try {
    const parsed = new URL(gatewayUrl, window.location.href);
    return (
      parsed.host !== window.location.host ||
      parsed.pathname.replace(/\/+$/, "") !== singleEntryRoute.wsBasePath
    );
  } catch {
    return false;
  }
}

export function saveSettings(next: UiSettings) {
  persistSettings(next);
}

function persistSettings(next: UiSettings) {
  persistSessionToken(next.gatewayUrl, next.token);
  const storage = getSafeLocalStorage();
  const scope = normalizeGatewayTokenScope(next.gatewayUrl);
  const scopedKey = settingsKeyForGateway(next.gatewayUrl);
  let existingSessionsByGateway: Record<string, ScopedSessionSelection> = {};
  try {
    // Try to migrate from legacy key or other scopes
    const raw =
      storage?.getItem(scopedKey) ??
      storage?.getItem(SETTINGS_KEY) ??
      storage?.getItem(`${SETTINGS_KEY_PREFIX}default`) ??
      storage?.getItem(legacySettingsKeyForGateway(next.gatewayUrl)) ??
      storage?.getItem(`${LEGACY_SETTINGS_KEY_PREFIX}default`) ??
      storage?.getItem(LEGACY_SETTINGS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as PersistedUiSettings;
      if (parsed.sessionsByGateway && typeof parsed.sessionsByGateway === "object") {
        existingSessionsByGateway = parsed.sessionsByGateway;
      }
    }
  } catch {
    // best-effort
  }
  const sessionsByGateway = Object.fromEntries(
    [
      ...Object.entries(existingSessionsByGateway).filter(([key]) => key !== scope),
      [
        scope,
        {
          sessionKey: next.sessionKey,
          lastActiveSessionKey: next.lastActiveSessionKey,
        },
      ],
    ].slice(-MAX_SCOPED_SESSION_ENTRIES),
  );
  const persisted: PersistedUiSettings = {
    gatewayUrl: next.gatewayUrl,
    theme: next.theme,
    themeMode: next.themeMode,
    chatFocusMode: next.chatFocusMode,
    chatShowThinking: next.chatShowThinking,
    chatShowToolCalls: next.chatShowToolCalls,
    splitRatio: next.splitRatio,
    navCollapsed: next.navCollapsed,
    navWidth: next.navWidth,
    navGroupsCollapsed: next.navGroupsCollapsed,
    borderRadius: next.borderRadius,
    sessionsByGateway,
    ...(next.locale ? { locale: next.locale } : {}),
  };
  const serialized = JSON.stringify(persisted);
  try {
    writeCompatStorageValue(
      "local",
      {
        current: scopedKey,
        legacy: [legacySettingsKeyForGateway(next.gatewayUrl), LEGACY_SETTINGS_KEY],
      },
      serialized,
    );
    storage?.setItem(SETTINGS_KEY, serialized);
  } catch {
    // best-effort — quota exceeded or security restrictions should not
    // prevent in-memory settings and visual updates from being applied
  }
}
