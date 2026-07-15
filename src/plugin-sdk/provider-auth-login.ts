// Public interactive auth/login helpers for provider plugins.

import { createLazyRuntimeMethodBinder, createLazyRuntimeModule } from "../shared/lazy-runtime.js";

const loadProviderAuthLoginRuntime = createLazyRuntimeModule(
  () => import("./provider-auth-login.runtime.js"),
);
const bindProviderAuthLoginRuntime = createLazyRuntimeMethodBinder(loadProviderAuthLoginRuntime);

// Phase 10.2: github-copilot plugin removed; the login command is no longer exported here.
export const loginChutes = bindProviderAuthLoginRuntime((runtime) => runtime.loginChutes);
export const loginOpenAICodexOAuth = bindProviderAuthLoginRuntime(
  (runtime) => runtime.loginOpenAICodexOAuth,
);
