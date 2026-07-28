/**
 * Persona memory hook.
 *
 * Runs when a session ends or is reset. Extracts relational signals (not task
 * content) into an evergreen per-user profile so the agent's persona does not
 * drift under long-context compaction.
 *
 * Output: <workspace>/memory/persona/<user_key>.md
 *   - No date in the path => memory-core temporal decay skips it (evergreen).
 *   - Compaction does not touch it; only this hook updates it.
 *
 * This layer is intentionally thin: transcript tail is resolved by the caller
 * (or the security-service), the LLM extraction runs server-side, and this hook
 * only handles file resolution, dispatch, and atomic write.
 */

import fs from "node:fs/promises";
import path from "node:path";

import { resolveHookConfig } from "../hook-config.js";
import { requestPersonaExtraction } from "./persona-client.js";

const DEFAULT_MESSAGES = 15;

/**
 * Resolve a stable per-user key for the persona filename.
 *
 * Accepts an explicit userKey when the caller already has one; otherwise falls
 * back to "default". The key is sanitized to a filename-safe form.
 */
export function resolveUserKey(userKey?: string): string {
  const trimmed = (userKey ?? "").trim();
  if (!trimmed) {
    return "default";
  }
  const safe = trimmed
    .replace(/[^a-zA-Z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 64);
  return safe || "default";
}

function personaProfilePath(workspaceDir: string, userKey: string): string {
  return path.join(workspaceDir, "memory", "persona", `${userKey}.md`);
}

async function readExistingProfile(profilePath: string): Promise<string> {
  try {
    return await fs.readFile(profilePath, "utf-8");
  } catch {
    return "";
  }
}

async function readTranscriptTail(
  sessionFile: string | undefined,
  messages: number,
): Promise<string | null> {
  if (!sessionFile) {
    return null;
  }
  try {
    const content = await fs.readFile(sessionFile, "utf-8");
    const lines = content.trim().split("\n");
    const collected: string[] = [];
    for (const line of lines) {
      try {
        const entry = JSON.parse(line) as { type?: string; message?: { role?: string; content?: unknown } };
        if (entry.type !== "message" || !entry.message) {
          continue;
        }
        const role = entry.message.role;
        if (role !== "user" && role !== "assistant") {
          continue;
        }
        const rawContent = entry.message.content;
        const text = Array.isArray(rawContent)
          ? (rawContent as Array<{ type?: string; text?: string }>).find((c) => c.type === "text")?.text
          : typeof rawContent === "string"
            ? rawContent
            : undefined;
        if (text && !text.startsWith("/")) {
          collected.push(`${role}: ${text}`);
        }
      } catch {
        // Skip invalid JSON lines.
      }
    }
    const tail = collected.slice(-messages).join("\n");
    return tail || null;
  } catch {
    return null;
  }
}

export type PersonaHookParams = {
  /** Workspace root that contains the `memory/` directory. */
  workspaceDir: string;
  /** Session transcript file (jsonl), if available. */
  sessionFile?: string;
  /** Stable per-user key for the profile filename. */
  userKey?: string;
  /** Session key, forwarded to the extraction service for provenance. */
  sessionKey?: string;
  /** Override the transcript tail length (default 15). */
  messages?: number;
  /** Override the hook config (mainly for tests). */
  forceEnabled?: boolean;
};

/**
 * Main hook entry point. Returns the path written, or null when nothing was
 * written (no transcript, extraction returned no signal, or disabled).
 *
 * Never throws — persona memory is best-effort and must not break the session
 * flow. Errors are surfaced via the return value's `error` field.
 */
export async function runPersonaMemoryHook(
  params: PersonaHookParams,
): Promise<{ written: string | null; error?: string }> {
  const config = resolveHookConfig();
  const enabled =
    params.forceEnabled ??
    (process.env.IRON_AGENT_PERSONA_MEMORY_ENABLED ?? "true") !== "false";

  if (!enabled) {
    return { written: null };
  }

  const messages =
    params.messages ??
    Number(process.env.IRON_AGENT_PERSONA_MEMORY_MESSAGES ?? DEFAULT_MESSAGES) ||
    DEFAULT_MESSAGES;

  try {
    const transcript = await readTranscriptTail(params.sessionFile, messages);
    if (!transcript) {
      return { written: null };
    }

    const userKey = resolveUserKey(params.userKey);
    const profilePath = personaProfilePath(params.workspaceDir, userKey);
    const existing = await readExistingProfile(profilePath);

    const merged = await requestPersonaExtraction(
      {
        session_key: params.sessionKey ?? "",
        user_key: userKey,
        messages,
        existing_profile: existing,
      },
      config,
    );

    if (!merged) {
      // No new persona signal this session — leave the existing profile alone.
      return { written: null };
    }

    await fs.mkdir(path.dirname(profilePath), { recursive: true });
    const header = existing
      ? ""
      : [
          `# Persona Profile — ${userKey}`,
          "",
          "_Evergreen profile maintained by the persona-memory hook. Compaction_",
          "_does not touch this file; update it only through session resets._",
          "",
          "",
        ].join("\n");
    const body = `${header}${merged.trim()}\n`;
    await fs.writeFile(profilePath, body, "utf-8");

    return { written: profilePath };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { written: null, error: message };
  }
}
