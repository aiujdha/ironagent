export function resolveDaemonContainerContext(
  env: Record<string, string | undefined> = process.env,
): string | null {
  return env.IRONAGENT_CONTAINER_HINT?.trim() || env.IRONAGENT_CONTAINER?.trim() || null;
}
