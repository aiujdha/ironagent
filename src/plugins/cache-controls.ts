export const DEFAULT_PLUGIN_DISCOVERY_CACHE_MS = 1000;
export const DEFAULT_PLUGIN_MANIFEST_CACHE_MS = 1000;

export function shouldUsePluginSnapshotCache(env: NodeJS.ProcessEnv): boolean {
  if (env.IRONAGENT_DISABLE_PLUGIN_DISCOVERY_CACHE?.trim()) {
    return false;
  }
  if (env.IRONAGENT_DISABLE_PLUGIN_MANIFEST_CACHE?.trim()) {
    return false;
  }
  const discoveryCacheMs = env.IRONAGENT_PLUGIN_DISCOVERY_CACHE_MS?.trim();
  if (discoveryCacheMs === "0") {
    return false;
  }
  const manifestCacheMs = env.IRONAGENT_PLUGIN_MANIFEST_CACHE_MS?.trim();
  if (manifestCacheMs === "0") {
    return false;
  }
  return true;
}

export function resolvePluginCacheMs(rawValue: string | undefined, defaultMs: number): number {
  const raw = rawValue?.trim();
  if (raw === "" || raw === "0") {
    return 0;
  }
  if (!raw) {
    return defaultMs;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) {
    return defaultMs;
  }
  return Math.max(0, parsed);
}

export function resolvePluginSnapshotCacheTtlMs(env: NodeJS.ProcessEnv): number {
  const discoveryCacheMs = resolvePluginCacheMs(
    env.IRONAGENT_PLUGIN_DISCOVERY_CACHE_MS,
    DEFAULT_PLUGIN_DISCOVERY_CACHE_MS,
  );
  const manifestCacheMs = resolvePluginCacheMs(
    env.IRONAGENT_PLUGIN_MANIFEST_CACHE_MS,
    DEFAULT_PLUGIN_MANIFEST_CACHE_MS,
  );
  return Math.min(discoveryCacheMs, manifestCacheMs);
}

export function buildPluginSnapshotCacheEnvKey(env: NodeJS.ProcessEnv) {
  return {
    IRONAGENT_BUNDLED_PLUGINS_DIR: env.IRONAGENT_BUNDLED_PLUGINS_DIR ?? "",
    IRONAGENT_DISABLE_PLUGIN_DISCOVERY_CACHE: env.IRONAGENT_DISABLE_PLUGIN_DISCOVERY_CACHE ?? "",
    IRONAGENT_DISABLE_PLUGIN_MANIFEST_CACHE: env.IRONAGENT_DISABLE_PLUGIN_MANIFEST_CACHE ?? "",
    IRONAGENT_PLUGIN_DISCOVERY_CACHE_MS: env.IRONAGENT_PLUGIN_DISCOVERY_CACHE_MS ?? "",
    IRONAGENT_PLUGIN_MANIFEST_CACHE_MS: env.IRONAGENT_PLUGIN_MANIFEST_CACHE_MS ?? "",
    IRONAGENT_HOME: env.IRONAGENT_HOME ?? "",
    IRONAGENT_STATE_DIR: env.IRONAGENT_STATE_DIR ?? "",
    IRONAGENT_CONFIG_PATH: env.IRONAGENT_CONFIG_PATH ?? "",
    HOME: env.HOME ?? "",
    USERPROFILE: env.USERPROFILE ?? "",
    VITEST: env.VITEST ?? "",
  };
}
