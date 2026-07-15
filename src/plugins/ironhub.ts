import {
  IronHubRequestError,
  downloadIronHubPackageArchive,
  fetchIronHubPackageDetail,
  fetchIronHubPackageVersion,
  parseIronHubPluginSpec,
  resolveLatestVersionFromPackage,
  satisfiesGatewayMinimum,
  satisfiesPluginApiRange,
  type IronHubPackageChannel,
  type IronHubPackageCompatibility,
  type IronHubPackageDetail,
  type IronHubPackageFamily,
} from "../infra/ironhub.js";
import { resolveCompatibilityHostVersion } from "../version.js";
import type { InstallSafetyOverrides } from "./install-security-scan.js";
import { installPluginFromArchive, type InstallPluginResult } from "./install.js";

export const IRONHUB_INSTALL_ERROR_CODE = {
  INVALID_SPEC: "invalid_spec",
  PACKAGE_NOT_FOUND: "package_not_found",
  VERSION_NOT_FOUND: "version_not_found",
  NO_INSTALLABLE_VERSION: "no_installable_version",
  SKILL_PACKAGE: "skill_package",
  UNSUPPORTED_FAMILY: "unsupported_family",
  PRIVATE_PACKAGE: "private_package",
  INCOMPATIBLE_PLUGIN_API: "incompatible_plugin_api",
  INCOMPATIBLE_GATEWAY: "incompatible_gateway",
} as const;

export type IronHubInstallErrorCode =
  (typeof IRONHUB_INSTALL_ERROR_CODE)[keyof typeof IRONHUB_INSTALL_ERROR_CODE];

type PluginInstallLogger = {
  info?: (message: string) => void;
  warn?: (message: string) => void;
};

export type IronHubPluginInstallRecordFields = {
  source: "ironhub";
  ironhubUrl: string;
  ironhubPackage: string;
  ironhubFamily: Exclude<IronHubPackageFamily, "skill">;
  ironhubChannel?: IronHubPackageChannel;
  version?: string;
  integrity?: string;
  resolvedAt?: string;
  installedAt?: string;
};

type IronHubInstallFailure = {
  ok: false;
  error: string;
  code?: IronHubInstallErrorCode;
};

export function formatIronHubSpecifier(params: { name: string; version?: string }): string {
  return `ironhub:${params.name}${params.version ? `@${params.version}` : ""}`;
}

function buildIronHubInstallFailure(
  error: string,
  code?: IronHubInstallErrorCode,
): IronHubInstallFailure {
  return { ok: false, error, code };
}

function mapIronHubRequestError(
  error: unknown,
  context: { stage: "package" | "version"; name: string; version?: string },
): IronHubInstallFailure {
  if (error instanceof IronHubRequestError && error.status === 404) {
    if (context.stage === "package") {
      return buildIronHubInstallFailure(
        "Package not found on IronHub.",
        IRONHUB_INSTALL_ERROR_CODE.PACKAGE_NOT_FOUND,
      );
    }
    return buildIronHubInstallFailure(
      `Version not found on IronHub: ${context.name}@${context.version ?? "unknown"}.`,
      IRONHUB_INSTALL_ERROR_CODE.VERSION_NOT_FOUND,
    );
  }
  return buildIronHubInstallFailure(error instanceof Error ? error.message : String(error));
}

function resolveRequestedVersion(params: {
  detail: IronHubPackageDetail;
  requestedVersion?: string;
}): string | null {
  if (params.requestedVersion) {
    return params.requestedVersion;
  }
  return resolveLatestVersionFromPackage(params.detail);
}

async function resolveCompatiblePackageVersion(params: {
  detail: IronHubPackageDetail;
  requestedVersion?: string;
  baseUrl?: string;
  token?: string;
}): Promise<
  | {
      ok: true;
      version: string;
      compatibility?: IronHubPackageCompatibility | null;
    }
  | IronHubInstallFailure
> {
  const version = resolveRequestedVersion(params);
  if (!version) {
    return buildIronHubInstallFailure(
      `IronHub package "${params.detail.package?.name ?? "unknown"}" has no installable version.`,
      IRONHUB_INSTALL_ERROR_CODE.NO_INSTALLABLE_VERSION,
    );
  }
  let versionDetail;
  try {
    versionDetail = await fetchIronHubPackageVersion({
      name: params.detail.package?.name ?? "",
      version,
      baseUrl: params.baseUrl,
      token: params.token,
    });
  } catch (error) {
    return mapIronHubRequestError(error, {
      stage: "version",
      name: params.detail.package?.name ?? "unknown",
      version,
    });
  }
  return {
    ok: true,
    version,
    compatibility:
      versionDetail.version?.compatibility ?? params.detail.package?.compatibility ?? null,
  };
}

function validateIronHubPluginPackage(params: {
  detail: IronHubPackageDetail;
  compatibility?: IronHubPackageCompatibility | null;
  runtimeVersion: string;
}): IronHubInstallFailure | null {
  const pkg = params.detail.package;
  if (!pkg) {
    return buildIronHubInstallFailure(
      "Package not found on IronHub.",
      IRONHUB_INSTALL_ERROR_CODE.PACKAGE_NOT_FOUND,
    );
  }
  if (pkg.family === "skill") {
    return buildIronHubInstallFailure(
      `"${pkg.name}" is a skill. Use "ironagent skills install ${pkg.name}" instead.`,
      IRONHUB_INSTALL_ERROR_CODE.SKILL_PACKAGE,
    );
  }
  if (pkg.family !== "code-plugin" && pkg.family !== "bundle-plugin") {
    return buildIronHubInstallFailure(
      `Unsupported IronHub package family: ${String(pkg.family)}`,
      IRONHUB_INSTALL_ERROR_CODE.UNSUPPORTED_FAMILY,
    );
  }
  if (pkg.channel === "private") {
    return buildIronHubInstallFailure(
      `"${pkg.name}" is private on IronHub and cannot be installed anonymously.`,
      IRONHUB_INSTALL_ERROR_CODE.PRIVATE_PACKAGE,
    );
  }

  const compatibility = params.compatibility;
  const runtimeVersion = params.runtimeVersion;
  if (
    compatibility?.pluginApiRange &&
    !satisfiesPluginApiRange(runtimeVersion, compatibility.pluginApiRange)
  ) {
    return buildIronHubInstallFailure(
      `Plugin "${pkg.name}" requires plugin API ${compatibility.pluginApiRange}, but this IronAgent runtime exposes ${runtimeVersion}.`,
      IRONHUB_INSTALL_ERROR_CODE.INCOMPATIBLE_PLUGIN_API,
    );
  }

  if (
    compatibility?.minGatewayVersion &&
    !satisfiesGatewayMinimum(runtimeVersion, compatibility.minGatewayVersion)
  ) {
    return buildIronHubInstallFailure(
      `Plugin "${pkg.name}" requires IronAgent >=${compatibility.minGatewayVersion}, but this host is ${runtimeVersion}.`,
      IRONHUB_INSTALL_ERROR_CODE.INCOMPATIBLE_GATEWAY,
    );
  }
  return null;
}

function logIronHubPackageSummary(params: {
  detail: IronHubPackageDetail;
  version: string;
  compatibility?: IronHubPackageCompatibility | null;
  logger?: PluginInstallLogger;
}) {
  const pkg = params.detail.package;
  if (!pkg) {
    return;
  }
  const verification = pkg.verification?.tier ? ` verification=${pkg.verification.tier}` : "";
  params.logger?.info?.(
    `IronHub ${pkg.family} ${pkg.name}@${params.version} channel=${pkg.channel}${verification}`,
  );
  const compatibilityParts = [
    params.compatibility?.pluginApiRange
      ? `pluginApi=${params.compatibility.pluginApiRange}`
      : null,
    params.compatibility?.minGatewayVersion
      ? `minGateway=${params.compatibility.minGatewayVersion}`
      : null,
  ].filter(Boolean);
  if (compatibilityParts.length > 0) {
    params.logger?.info?.(`Compatibility: ${compatibilityParts.join(" ")}`);
  }
  if (pkg.channel !== "official") {
    params.logger?.warn?.(
      `IronHub package "${pkg.name}" is ${pkg.channel}; review source and verification before enabling.`,
    );
  }
}

export async function installPluginFromIronHub(
  params: InstallSafetyOverrides & {
    spec: string;
    baseUrl?: string;
    token?: string;
    logger?: PluginInstallLogger;
    mode?: "install" | "update";
    dryRun?: boolean;
    expectedPluginId?: string;
  },
): Promise<
  | ({
      ok: true;
    } & Extract<InstallPluginResult, { ok: true }> & {
        ironhub: IronHubPluginInstallRecordFields;
        packageName: string;
      })
  | IronHubInstallFailure
  | Extract<InstallPluginResult, { ok: false }>
> {
  const parsed = parseIronHubPluginSpec(params.spec);
  if (!parsed?.name) {
    return buildIronHubInstallFailure(
      `invalid IronHub plugin spec: ${params.spec}`,
      IRONHUB_INSTALL_ERROR_CODE.INVALID_SPEC,
    );
  }

  params.logger?.info?.(`Resolving ${formatIronHubSpecifier(parsed)}…`);
  let detail: IronHubPackageDetail;
  try {
    detail = await fetchIronHubPackageDetail({
      name: parsed.name,
      baseUrl: params.baseUrl,
      token: params.token,
    });
  } catch (error) {
    return mapIronHubRequestError(error, {
      stage: "package",
      name: parsed.name,
    });
  }
  const versionState = await resolveCompatiblePackageVersion({
    detail,
    requestedVersion: parsed.version,
    baseUrl: params.baseUrl,
    token: params.token,
  });
  if (!versionState.ok) {
    return versionState;
  }
  const runtimeVersion = resolveCompatibilityHostVersion();
  const validationFailure = validateIronHubPluginPackage({
    detail,
    compatibility: versionState.compatibility,
    runtimeVersion,
  });
  if (validationFailure) {
    return validationFailure;
  }
  logIronHubPackageSummary({
    detail,
    version: versionState.version,
    compatibility: versionState.compatibility,
    logger: params.logger,
  });

  let archive;
  try {
    archive = await downloadIronHubPackageArchive({
      name: parsed.name,
      version: versionState.version,
      baseUrl: params.baseUrl,
      token: params.token,
    });
  } catch (error) {
    return buildIronHubInstallFailure(error instanceof Error ? error.message : String(error));
  }
  try {
    params.logger?.info?.(
      `Downloading ${detail.package?.family === "bundle-plugin" ? "bundle" : "plugin"} ${parsed.name}@${versionState.version} from IronHub…`,
    );
    const installResult = await installPluginFromArchive({
      archivePath: archive.archivePath,
      dangerouslyForceUnsafeInstall: params.dangerouslyForceUnsafeInstall,
      logger: params.logger,
      mode: params.mode,
      dryRun: params.dryRun,
      expectedPluginId: params.expectedPluginId,
    });
    if (!installResult.ok) {
      return installResult;
    }

    const pkg = detail.package!;
    const ironhubFamily =
      pkg.family === "code-plugin" || pkg.family === "bundle-plugin" ? pkg.family : null;
    if (!ironhubFamily) {
      return buildIronHubInstallFailure(
        `Unsupported IronHub package family: ${pkg.family}`,
        IRONHUB_INSTALL_ERROR_CODE.UNSUPPORTED_FAMILY,
      );
    }
    return {
      ...installResult,
      packageName: parsed.name,
      ironhub: {
        source: "ironhub",
        ironhubUrl:
          params.baseUrl?.trim() ||
          process.env.IRONAGENT_IRONHUB_URL?.trim() ||
          "https://ironhub.ai",
        ironhubPackage: parsed.name,
        ironhubFamily,
        ironhubChannel: pkg.channel,
        version: installResult.version ?? versionState.version,
        integrity: archive.integrity,
        resolvedAt: new Date().toISOString(),
      },
    };
  } finally {
    await archive.cleanup().catch(() => undefined);
  }
}
