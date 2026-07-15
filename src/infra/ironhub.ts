import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { ExternalPluginCompatibility } from "../../packages/plugin-package-contract/src/index.js";
import { isAtLeast, parseSemver } from "./runtime-guard.js";
import { compareComparableSemver, parseComparableSemver } from "./semver-compare.js";
import { createTempDownloadTarget } from "./temp-download.js";

const DEFAULT_IRONHUB_URL = "https://ironhub.ai";
const DEFAULT_FETCH_TIMEOUT_MS = 30_000;

export type IronHubPackageFamily = "skill" | "code-plugin" | "bundle-plugin";
export type IronHubPackageChannel = "official" | "community" | "private";
export type IronHubPackageCompatibility = ExternalPluginCompatibility;
export type IronHubPackageListItem = {
  name: string;
  displayName: string;
  family: IronHubPackageFamily;
  runtimeId?: string | null;
  channel: IronHubPackageChannel;
  isOfficial: boolean;
  summary?: string | null;
  ownerHandle?: string | null;
  createdAt: number;
  updatedAt: number;
  latestVersion?: string | null;
  capabilityTags?: string[];
  executesCode?: boolean;
  verificationTier?: string | null;
};
export type IronHubPackageDetail = {
  package:
    | (IronHubPackageListItem & {
        tags?: Record<string, string>;
        compatibility?: IronHubPackageCompatibility | null;
        capabilities?: {
          executesCode?: boolean;
          runtimeId?: string;
          capabilityTags?: string[];
          bundleFormat?: string;
          hostTargets?: string[];
          pluginKind?: string;
          channels?: string[];
          providers?: string[];
          hooks?: string[];
          bundledSkills?: string[];
        } | null;
        verification?: {
          tier?: string;
          scope?: string;
          summary?: string;
          sourceRepo?: string;
          sourceCommit?: string;
          hasProvenance?: boolean;
          scanStatus?: string;
        } | null;
      })
    | null;
  owner?: {
    handle?: string | null;
    displayName?: string | null;
    image?: string | null;
  } | null;
};

export type IronHubPackageVersion = {
  package: {
    name: string;
    displayName: string;
    family: IronHubPackageFamily;
  } | null;
  version: {
    version: string;
    createdAt: number;
    changelog: string;
    distTags?: string[];
    files?: unknown;
    compatibility?: IronHubPackageCompatibility | null;
    capabilities?: IronHubPackageDetail["package"] extends infer T
      ? T extends { capabilities?: infer C }
        ? C
        : never
      : never;
    verification?: IronHubPackageDetail["package"] extends infer T
      ? T extends { verification?: infer C }
        ? C
        : never
      : never;
  } | null;
};

export type IronHubPackageSearchResult = {
  score: number;
  package: IronHubPackageListItem;
};

export type IronHubSkillSearchResult = {
  score: number;
  slug: string;
  displayName: string;
  summary?: string;
  version?: string;
  updatedAt?: number;
};

export type IronHubSkillDetail = {
  skill: {
    slug: string;
    displayName: string;
    summary?: string;
    tags?: Record<string, string>;
    createdAt: number;
    updatedAt: number;
  } | null;
  latestVersion?: {
    version: string;
    createdAt: number;
    changelog?: string;
  } | null;
  metadata?: {
    os?: string[] | null;
    systems?: string[] | null;
  } | null;
  owner?: {
    handle?: string | null;
    displayName?: string | null;
    image?: string | null;
  } | null;
};

export type IronHubSkillListResponse = {
  items: Array<{
    slug: string;
    displayName: string;
    summary?: string;
    tags?: Record<string, string>;
    latestVersion?: {
      version: string;
      createdAt: number;
      changelog?: string;
    } | null;
    metadata?: {
      os?: string[] | null;
      systems?: string[] | null;
    } | null;
    createdAt: number;
    updatedAt: number;
  }>;
  nextCursor?: string | null;
};

export type IronHubDownloadResult = {
  archivePath: string;
  integrity: string;
  cleanup: () => Promise<void>;
};

type FetchLike = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

type IronHubRequestParams = {
  baseUrl?: string;
  path: string;
  token?: string;
  timeoutMs?: number;
  search?: Record<string, string | undefined>;
  fetchImpl?: FetchLike;
};

type IronHubConfigLike = {
  token?: unknown;
  accessToken?: unknown;
  authToken?: unknown;
  apiToken?: unknown;
  auth?: IronHubConfigLike | null;
  session?: IronHubConfigLike | null;
  credentials?: IronHubConfigLike | null;
  user?: IronHubConfigLike | null;
};

export class IronHubRequestError extends Error {
  readonly status: number;
  readonly requestPath: string;
  readonly responseBody: string;

  constructor(params: { path: string; status: number; body: string }) {
    super(`IronHub ${params.path} failed (${params.status}): ${params.body}`);
    this.name = "IronHubRequestError";
    this.status = params.status;
    this.requestPath = params.path;
    this.responseBody = params.body;
  }
}

function normalizeBaseUrl(baseUrl?: string): string {
  const envValue =
    process.env.IRONAGENT_IRONHUB_URL?.trim() ||
    process.env.IRONHUB_URL?.trim() ||
    DEFAULT_IRONHUB_URL;
  const value = (baseUrl?.trim() || envValue).replace(/\/+$/, "");
  return value || DEFAULT_IRONHUB_URL;
}

function readNonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function extractTokenFromIronHubConfig(value: unknown): string | undefined {
  if (!value || typeof value !== "object") {
    return undefined;
  }
  const record = value as IronHubConfigLike;
  return (
    readNonEmptyString(record.accessToken) ??
    readNonEmptyString(record.authToken) ??
    readNonEmptyString(record.apiToken) ??
    readNonEmptyString(record.token) ??
    extractTokenFromIronHubConfig(record.auth) ??
    extractTokenFromIronHubConfig(record.session) ??
    extractTokenFromIronHubConfig(record.credentials) ??
    extractTokenFromIronHubConfig(record.user)
  );
}

function resolveIronHubConfigPaths(): string[] {
  const explicit =
    process.env.IRONAGENT_IRONHUB_CONFIG_PATH?.trim() ||
    process.env.IRONHUB_CONFIG_PATH?.trim();
  if (explicit) {
    return [explicit];
  }

  const xdgConfigHome = process.env.XDG_CONFIG_HOME?.trim();
  const configHome =
    xdgConfigHome && xdgConfigHome.length > 0 ? xdgConfigHome : path.join(os.homedir(), ".config");
  const xdgPath = path.join(configHome, "ironhub", "config.json");

  if (process.platform === "darwin") {
    return [
      path.join(os.homedir(), "Library", "Application Support", "ironhub", "config.json"),
      xdgPath,
    ];
  }

  return [xdgPath];
}

export async function resolveIronHubAuthToken(): Promise<string | undefined> {
  const envToken =
    process.env.IRONAGENT_IRONHUB_TOKEN?.trim() ||
    process.env.IRONHUB_TOKEN?.trim() ||
    process.env.IRONHUB_AUTH_TOKEN?.trim();
  if (envToken) {
    return envToken;
  }

  for (const configPath of resolveIronHubConfigPaths()) {
    try {
      const raw = await fs.readFile(configPath, "utf8");
      const token = extractTokenFromIronHubConfig(JSON.parse(raw));
      if (token) {
        return token;
      }
    } catch {
      // Try the next candidate path.
    }
  }
  return undefined;
}

function compareSemver(left: string, right: string): number | null {
  return compareComparableSemver(parseComparableSemver(left), parseComparableSemver(right));
}

function upperBoundForCaret(version: string): string | null {
  const parsed = parseComparableSemver(version);
  if (!parsed) {
    return null;
  }
  if (parsed.major > 0) {
    return `${parsed.major + 1}.0.0`;
  }
  if (parsed.minor > 0) {
    return `0.${parsed.minor + 1}.0`;
  }
  return `0.0.${parsed.patch + 1}`;
}

function satisfiesComparator(version: string, token: string): boolean {
  const trimmed = token.trim();
  if (!trimmed) {
    return true;
  }
  if (trimmed.startsWith("^")) {
    const base = trimmed.slice(1).trim();
    const upperBound = upperBoundForCaret(base);
    const lowerCmp = compareSemver(version, base);
    const upperCmp = upperBound ? compareSemver(version, upperBound) : null;
    return lowerCmp != null && upperCmp != null && lowerCmp >= 0 && upperCmp < 0;
  }

  const match = /^(>=|<=|>|<|=)?\s*(.+)$/.exec(trimmed);
  if (!match) {
    return false;
  }
  const operator = match[1] ?? "=";
  const target = match[2]?.trim();
  if (!target) {
    return false;
  }
  const cmp = compareSemver(version, target);
  if (cmp == null) {
    return false;
  }
  switch (operator) {
    case ">=":
      return cmp >= 0;
    case "<=":
      return cmp <= 0;
    case ">":
      return cmp > 0;
    case "<":
      return cmp < 0;
    case "=":
    default:
      return cmp === 0;
  }
}

function satisfiesSemverRange(version: string, range: string): boolean {
  const tokens = range
    .trim()
    .split(/\s+/)
    .map((token) => token.trim())
    .filter(Boolean);
  if (tokens.length === 0) {
    return false;
  }
  return tokens.every((token) => satisfiesComparator(version, token));
}

function buildUrl(params: Pick<IronHubRequestParams, "baseUrl" | "path" | "search">): URL {
  const url = new URL(params.path, `${normalizeBaseUrl(params.baseUrl)}/`);
  for (const [key, value] of Object.entries(params.search ?? {})) {
    if (!value) {
      continue;
    }
    url.searchParams.set(key, value);
  }
  return url;
}

async function ironhubRequest(
  params: IronHubRequestParams,
): Promise<{ response: Response; url: URL }> {
  const url = buildUrl(params);
  const token = params.token?.trim() || (await resolveIronHubAuthToken());
  const controller = new AbortController();
  const timeout = setTimeout(
    () =>
      controller.abort(
        new Error(
          `IronHub request timed out after ${params.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS}ms`,
        ),
      ),
    params.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS,
  );
  try {
    const response = await (params.fetchImpl ?? fetch)(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      signal: controller.signal,
    });
    return { response, url };
  } finally {
    clearTimeout(timeout);
  }
}

async function readErrorBody(response: Response): Promise<string> {
  try {
    const text = (await response.text()).trim();
    return text || response.statusText || `HTTP ${response.status}`;
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}

async function fetchJson<T>(params: IronHubRequestParams): Promise<T> {
  const { response, url } = await ironhubRequest(params);
  if (!response.ok) {
    throw new IronHubRequestError({
      path: url.pathname,
      status: response.status,
      body: await readErrorBody(response),
    });
  }
  return (await response.json()) as T;
}

export function resolveIronHubBaseUrl(baseUrl?: string): string {
  return normalizeBaseUrl(baseUrl);
}

export function formatSha256Integrity(bytes: Uint8Array): string {
  const digest = createHash("sha256").update(bytes).digest("base64");
  return `sha256-${digest}`;
}

export function parseIronHubPluginSpec(raw: string): {
  name: string;
  version?: string;
  baseUrl?: string;
} | null {
  const trimmed = raw.trim();
  if (!trimmed.toLowerCase().startsWith("ironhub:")) {
    return null;
  }
  const spec = trimmed.slice("ironhub:".length).trim();
  if (!spec) {
    return null;
  }
  const atIndex = spec.lastIndexOf("@");
  if (atIndex <= 0 || atIndex >= spec.length - 1) {
    return { name: spec };
  }
  return {
    name: spec.slice(0, atIndex).trim(),
    version: spec.slice(atIndex + 1).trim() || undefined,
  };
}

export async function fetchIronHubPackageDetail(params: {
  name: string;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
}): Promise<IronHubPackageDetail> {
  return await fetchJson<IronHubPackageDetail>({
    baseUrl: params.baseUrl,
    path: `/api/v1/packages/${encodeURIComponent(params.name)}`,
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
  });
}

export async function fetchIronHubPackageVersion(params: {
  name: string;
  version: string;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
}): Promise<IronHubPackageVersion> {
  return await fetchJson<IronHubPackageVersion>({
    baseUrl: params.baseUrl,
    path: `/api/v1/packages/${encodeURIComponent(params.name)}/versions/${encodeURIComponent(
      params.version,
    )}`,
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
  });
}

export async function searchIronHubPackages(params: {
  query: string;
  family?: IronHubPackageFamily;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
  limit?: number;
}): Promise<IronHubPackageSearchResult[]> {
  const result = await fetchJson<{ results: IronHubPackageSearchResult[] }>({
    baseUrl: params.baseUrl,
    path: "/api/v1/packages/search",
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
    search: {
      q: params.query.trim(),
      family: params.family,
      limit: params.limit ? String(params.limit) : undefined,
    },
  });
  return result.results ?? [];
}

export async function searchIronHubSkills(params: {
  query: string;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
  limit?: number;
}): Promise<IronHubSkillSearchResult[]> {
  const result = await fetchJson<{ results: IronHubSkillSearchResult[] }>({
    baseUrl: params.baseUrl,
    path: "/api/v1/search",
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
    search: {
      q: params.query.trim(),
      limit: params.limit ? String(params.limit) : undefined,
    },
  });
  return result.results ?? [];
}

export async function fetchIronHubSkillDetail(params: {
  slug: string;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
}): Promise<IronHubSkillDetail> {
  return await fetchJson<IronHubSkillDetail>({
    baseUrl: params.baseUrl,
    path: `/api/v1/skills/${encodeURIComponent(params.slug)}`,
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
  });
}

export async function listIronHubSkills(params: {
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
  limit?: number;
}): Promise<IronHubSkillListResponse> {
  return await fetchJson<IronHubSkillListResponse>({
    baseUrl: params.baseUrl,
    path: "/api/v1/skills",
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
    search: {
      limit: params.limit ? String(params.limit) : undefined,
    },
  });
}

export async function downloadIronHubPackageArchive(params: {
  name: string;
  version?: string;
  tag?: string;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
}): Promise<IronHubDownloadResult> {
  const search = params.version
    ? { version: params.version }
    : params.tag
      ? { tag: params.tag }
      : undefined;
  const { response, url } = await ironhubRequest({
    baseUrl: params.baseUrl,
    path: `/api/v1/packages/${encodeURIComponent(params.name)}/download`,
    search,
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
  });
  if (!response.ok) {
    throw new IronHubRequestError({
      path: url.pathname,
      status: response.status,
      body: await readErrorBody(response),
    });
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  const target = await createTempDownloadTarget({
    prefix: "ironagent-ironhub-package",
    fileName: `${params.name}.zip`,
    tmpDir: os.tmpdir(),
  });
  await fs.writeFile(target.path, bytes);
  return {
    archivePath: target.path,
    integrity: formatSha256Integrity(bytes),
    cleanup: target.cleanup,
  };
}

export async function downloadIronHubSkillArchive(params: {
  slug: string;
  version?: string;
  tag?: string;
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: FetchLike;
}): Promise<IronHubDownloadResult> {
  const { response, url } = await ironhubRequest({
    baseUrl: params.baseUrl,
    path: "/api/v1/download",
    token: params.token,
    timeoutMs: params.timeoutMs,
    fetchImpl: params.fetchImpl,
    search: {
      slug: params.slug,
      version: params.version,
      tag: params.version ? undefined : params.tag,
    },
  });
  if (!response.ok) {
    throw new IronHubRequestError({
      path: url.pathname,
      status: response.status,
      body: await readErrorBody(response),
    });
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  const target = await createTempDownloadTarget({
    prefix: "ironagent-ironhub-skill",
    fileName: `${params.slug}.zip`,
    tmpDir: os.tmpdir(),
  });
  await fs.writeFile(target.path, bytes);
  return {
    archivePath: target.path,
    integrity: formatSha256Integrity(bytes),
    cleanup: target.cleanup,
  };
}

export function resolveLatestVersionFromPackage(detail: IronHubPackageDetail): string | null {
  return detail.package?.latestVersion ?? detail.package?.tags?.latest ?? null;
}

export function isIronHubFamilySkill(detail: IronHubPackageDetail | IronHubSkillDetail): boolean {
  if ("package" in detail) {
    return detail.package?.family === "skill";
  }
  return Boolean(detail.skill);
}

export function satisfiesPluginApiRange(
  pluginApiVersion: string,
  pluginApiRange?: string | null,
): boolean {
  if (!pluginApiRange) {
    return true;
  }
  return satisfiesSemverRange(pluginApiVersion, pluginApiRange);
}

export function satisfiesGatewayMinimum(
  currentVersion: string,
  minGatewayVersion?: string | null,
): boolean {
  if (!minGatewayVersion) {
    return true;
  }
  const current = parseSemver(currentVersion);
  const minimum = parseSemver(minGatewayVersion);
  if (!current || !minimum) {
    return false;
  }
  return isAtLeast(current, minimum);
}
