import { resolveHookConfig, type HookConfig } from "./hook-config.js";
import type {
  PostExecuteHookRequest,
  PostExecuteHookResponse,
  PreExecuteHookRequest,
  PreExecuteHookResponse,
} from "./types.js";

async function postJson<TRequest, TResponse>(
  path: string,
  body: TRequest,
  config: HookConfig,
): Promise<TResponse> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.timeoutMs);

  try {
    const response = await fetch(`${config.baseUrl}${path}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(config.apiToken ? { authorization: `Bearer ${config.apiToken}` } : {}),
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`hook request failed with status ${response.status}`);
    }

    return (await response.json()) as TResponse;
  } finally {
    clearTimeout(timeout);
  }

export async function sendPreExecuteHook(
  request: PreExecuteHookRequest,
  config: HookConfig = resolveHookConfig(),
): Promise<PreExecuteHookResponse> {
  return await postJson<PreExecuteHookRequest, PreExecuteHookResponse>(
    "/api/v1/hooks/pre-execute",
    request,
    config,
  );
}

export async function sendPostExecuteHook(
  request: PostExecuteHookRequest,
  config: HookConfig = resolveHookConfig(),
): Promise<PostExecuteHookResponse> {
  return await postJson<PostExecuteHookRequest, PostExecuteHookResponse>(
    "/api/v1/hooks/post-execute",
    request,
    config,
  );
}
