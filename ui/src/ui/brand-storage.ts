import { getSafeLocalStorage, getSafeSessionStorage } from "../local-storage.ts";

export type CompatStorageArea = "local" | "session";

export type CompatStorageKeySpec = {
  current: string;
  legacy: string | string[];
};

type ReadCompatStorageOptions = {
  migrateLegacy?: boolean;
};

type WriteCompatStorageOptions = {
  mirrorLegacy?: boolean;
};

function getStorage(area: CompatStorageArea): Storage | null {
  return area === "local" ? getSafeLocalStorage() : getSafeSessionStorage();
}

function normalizeLegacyKeys(spec: CompatStorageKeySpec): string[] {
  const keys = Array.isArray(spec.legacy) ? spec.legacy : [spec.legacy];
  return keys.filter((key) => key && key !== spec.current);
}

function setStorageValue(storage: Storage | null, key: string, value: string) {
  try {
    storage?.setItem(key, value);
  } catch {
    // Ignore quota/private-mode failures.
  }
}

function removeStorageValue(storage: Storage | null, key: string) {
  try {
    storage?.removeItem(key);
  } catch {
    // Ignore quota/private-mode failures.
  }
}

export function readCompatStorageValue(
  area: CompatStorageArea,
  spec: CompatStorageKeySpec,
  options: ReadCompatStorageOptions = {},
): string | null {
  const storage = getStorage(area);
  if (!storage) {
    return null;
  }

  const currentValue = storage.getItem(spec.current);
  if (currentValue != null) {
    return currentValue;
  }

  const legacyKeys = normalizeLegacyKeys(spec);
  for (const key of legacyKeys) {
    const legacyValue = storage.getItem(key);
    if (legacyValue == null) {
      continue;
    }
    if (options.migrateLegacy !== false) {
      setStorageValue(storage, spec.current, legacyValue);
    }
    return legacyValue;
  }

  return null;
}

export function writeCompatStorageValue(
  area: CompatStorageArea,
  spec: CompatStorageKeySpec,
  value: string | null,
  options: WriteCompatStorageOptions = {},
) {
  const storage = getStorage(area);
  if (!storage) {
    return;
  }

  if (value == null) {
    removeCompatStorageValue(area, spec);
    return;
  }

  setStorageValue(storage, spec.current, value);
  if (options.mirrorLegacy === false) {
    return;
  }
  for (const key of normalizeLegacyKeys(spec)) {
    setStorageValue(storage, key, value);
  }
}

export function removeCompatStorageValue(area: CompatStorageArea, spec: CompatStorageKeySpec) {
  const storage = getStorage(area);
  if (!storage) {
    return;
  }

  removeStorageValue(storage, spec.current);
  for (const key of normalizeLegacyKeys(spec)) {
    removeStorageValue(storage, key);
  }
}
