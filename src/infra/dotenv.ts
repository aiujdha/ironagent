import fs from "node:fs";
import path from "node:path";
import dotenv from "dotenv";
import { resolveConfigDir } from "../utils.js";
import {
  isDangerousHostEnvOverrideVarName,
  isDangerousHostEnvVarName,
  normalizeEnvVarKey,
} from "./host-env-security.js";

const BLOCKED_WORKSPACE_DOTENV_KEYS = new Set([
  "ALL_PROXY",
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_OAUTH_TOKEN",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NODE_TLS_REJECT_UNAUTHORIZED",
  "NO_PROXY",
  "IRONAGENT_AGENT_DIR",
  "IRONAGENT_BUNDLED_HOOKS_DIR",
  "IRONAGENT_BUNDLED_PLUGINS_DIR",
  "IRONAGENT_BUNDLED_SKILLS_DIR",
  "IRONAGENT_CONFIG_PATH",
  "IRONAGENT_GATEWAY_PASSWORD",
  "IRONAGENT_GATEWAY_SECRET",
  "IRONAGENT_GATEWAY_TOKEN",
  "IRONAGENT_HOME",
  "IRONAGENT_LIVE_ANTHROPIC_KEY",
  "IRONAGENT_LIVE_ANTHROPIC_KEYS",
  "IRONAGENT_LIVE_GEMINI_KEY",
  "IRONAGENT_LIVE_OPENAI_KEY",
  "IRONAGENT_OAUTH_DIR",
  "IRONAGENT_PROFILE",
  "IRONAGENT_STATE_DIR",
  "OPENAI_API_KEY",
  "OPENAI_API_KEYS",
  "PI_CODING_AGENT_DIR",
]);

const BLOCKED_WORKSPACE_DOTENV_SUFFIXES = ["_BASE_URL"];
const BLOCKED_WORKSPACE_DOTENV_PREFIXES = ["ANTHROPIC_API_KEY_", "OPENAI_API_KEY_"];

function shouldBlockWorkspaceRuntimeDotEnvKey(key: string): boolean {
  return isDangerousHostEnvVarName(key) || isDangerousHostEnvOverrideVarName(key);
}

function shouldBlockRuntimeDotEnvKey(key: string): boolean {
  // The global ~/.ironagent/.env (or IRONAGENT_STATE_DIR/.env) is a trusted
  // operator-controlled runtime surface. Workspace .env is untrusted and gets
  // the strict blocklist, but the trusted global fallback is allowed to set
  // runtime vars like proxy/base-url/auth values.
  void key;
  return false;
}

function shouldBlockWorkspaceDotEnvKey(key: string): boolean {
  const upper = key.toUpperCase();
  return (
    shouldBlockWorkspaceRuntimeDotEnvKey(upper) ||
    BLOCKED_WORKSPACE_DOTENV_KEYS.has(upper) ||
    BLOCKED_WORKSPACE_DOTENV_PREFIXES.some((prefix) => upper.startsWith(prefix)) ||
    BLOCKED_WORKSPACE_DOTENV_SUFFIXES.some((suffix) => upper.endsWith(suffix))
  );
}

function loadDotEnvFile(params: {
  filePath: string;
  shouldBlockKey: (key: string) => boolean;
  quiet?: boolean;
}) {
  let content: string;
  try {
    content = fs.readFileSync(params.filePath, "utf8");
  } catch (error) {
    if (!params.quiet) {
      const code =
        error && typeof error === "object" && "code" in error ? String(error.code) : undefined;
      if (code !== "ENOENT") {
        console.warn(`[dotenv] Failed to read ${params.filePath}: ${String(error)}`);
      }
    }
    return;
  }

  let parsed: Record<string, string>;
  try {
    parsed = dotenv.parse(content);
  } catch (error) {
    if (!params.quiet) {
      console.warn(`[dotenv] Failed to parse ${params.filePath}: ${String(error)}`);
    }
    return;
  }
  for (const [rawKey, value] of Object.entries(parsed)) {
    const key = normalizeEnvVarKey(rawKey, { portable: true });
    if (!key || params.shouldBlockKey(key)) {
      continue;
    }
    if (process.env[key] !== undefined) {
      continue;
    }
    process.env[key] = value;
  }
}

export function loadRuntimeDotEnvFile(filePath: string, opts?: { quiet?: boolean }) {
  loadDotEnvFile({
    filePath,
    shouldBlockKey: shouldBlockRuntimeDotEnvKey,
    quiet: opts?.quiet ?? true,
  });
}

export function loadWorkspaceDotEnvFile(filePath: string, opts?: { quiet?: boolean }) {
  loadDotEnvFile({
    filePath,
    shouldBlockKey: shouldBlockWorkspaceDotEnvKey,
    quiet: opts?.quiet ?? true,
  });
}

export function loadDotEnv(opts?: { quiet?: boolean }) {
  const quiet = opts?.quiet ?? true;
  const cwdEnvPath = path.join(process.cwd(), ".env");
  loadWorkspaceDotEnvFile(cwdEnvPath, { quiet });

  // Then load global fallback: ~/.ironagent/.env (or IRONAGENT_STATE_DIR/.env),
  // without overriding any env vars already present.
  const globalEnvPath = path.join(resolveConfigDir(process.env), ".env");
  loadRuntimeDotEnvFile(globalEnvPath, { quiet });
}
