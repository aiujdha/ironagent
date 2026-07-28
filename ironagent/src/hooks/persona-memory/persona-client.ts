/**
 * HTTP client for the persona-memory extraction endpoint.
 *
 * Mirrors the style of hook-client.ts: a small, dependency-free POST helper
 * with an AbortController-based timeout. The actual LLM extraction runs in the
 * Python security-service so this layer stays thin and testable.
 */

import { resolveHookConfig, type HookConfig } from "../hook-config.js";

/** Sentinel returned by the service when a session had no persona signal. */
export const NO_NEW_PROFILE = null;

export type PersonaExtractRequest = {
  session_key: string;
  user_key: string;
  messages: number;
  existing_profile: string;
};

export type PersonaExtractResponse = {
  /** Merged markdown profile, or null when there is nothing new to record. */
  profile: string | null;
};

/**
 * Call the persona extraction endpoint.
 *
 * Returns the merged profile string, or null when the service reports no new
 * signal (or when the request fails and fail-closed is off — persona memory is
 * best-effort and must never break the session flow).
 */
export async function requestPersonaExtraction(
  request: PersonaExtractRequest,
  config: HookConfig = resolveHookConfig(),
): Promise<string | null> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.timeoutMs);

  try {
    const response = await fetch(`${config.baseUrl}/api/v1/hooks/persona-extract`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(config.apiToken ? { authorization: `Bearer ${config.apiToken}` } : {}),
      },
      body: JSON.stringify(request),
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`persona-extract request failed with status ${response.status}`);
    }

    const data = (await response.json()) as PersonaExtractResponse;
    return data.profile ?? null;
  } finally {
    clearTimeout(timeout);
  }
}
