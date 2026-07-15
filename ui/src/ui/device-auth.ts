import {
  clearDeviceAuthTokenFromStore,
  type DeviceAuthEntry,
  loadDeviceAuthTokenFromStore,
  storeDeviceAuthTokenInStore,
} from "../../../src/shared/device-auth-store.js";
import type { DeviceAuthStore } from "../../../src/shared/device-auth.js";
import {
  readCompatStorageValue,
  writeCompatStorageValue,
  removeCompatStorageValue,
} from "./brand-storage.ts";

const STORAGE_KEY = "ironagent.device.auth.v1";
const LEGACY_STORAGE_KEY = "ironagent.device.auth.v1";

function readStore(): DeviceAuthStore | null {
  try {
    const raw = readCompatStorageValue(
      "local",
      {
        current: STORAGE_KEY,
        legacy: LEGACY_STORAGE_KEY,
      },
      { migrateLegacy: true },
    );
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as DeviceAuthStore;
    if (!parsed || parsed.version !== 1) {
      return null;
    }
    if (!parsed.deviceId || typeof parsed.deviceId !== "string") {
      return null;
    }
    if (!parsed.tokens || typeof parsed.tokens !== "object") {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function writeStore(store: DeviceAuthStore) {
  try {
    writeCompatStorageValue(
      "local",
      {
        current: STORAGE_KEY,
        legacy: LEGACY_STORAGE_KEY,
      },
      JSON.stringify(store),
    );
  } catch {
    // best-effort
  }
}

export function loadDeviceAuthToken(params: {
  deviceId: string;
  role: string;
}): DeviceAuthEntry | null {
  return loadDeviceAuthTokenFromStore({
    adapter: { readStore, writeStore },
    deviceId: params.deviceId,
    role: params.role,
  });
}

export function storeDeviceAuthToken(params: {
  deviceId: string;
  role: string;
  token: string;
  scopes?: string[];
}): DeviceAuthEntry {
  return storeDeviceAuthTokenInStore({
    adapter: { readStore, writeStore },
    deviceId: params.deviceId,
    role: params.role,
    token: params.token,
    scopes: params.scopes,
  });
}

export function clearDeviceAuthToken(params: { deviceId: string; role: string }) {
  clearDeviceAuthTokenFromStore({
    adapter: { readStore, writeStore },
    deviceId: params.deviceId,
    role: params.role,
  });
  try {
    const remaining = readStore();
    if (!remaining || Object.keys(remaining.tokens).length === 0) {
      removeCompatStorageValue("local", {
        current: STORAGE_KEY,
        legacy: LEGACY_STORAGE_KEY,
      });
    }
  } catch {
    // best-effort
  }
}
