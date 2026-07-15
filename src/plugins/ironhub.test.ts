import { beforeEach, describe, expect, it, vi } from "vitest";

const parseIronHubPluginSpecMock = vi.fn();
const fetchIronHubPackageDetailMock = vi.fn();
const fetchIronHubPackageVersionMock = vi.fn();
const downloadIronHubPackageArchiveMock = vi.fn();
const archiveCleanupMock = vi.fn();
const resolveLatestVersionFromPackageMock = vi.fn();
const resolveCompatibilityHostVersionMock = vi.fn();
const installPluginFromArchiveMock = vi.fn();

vi.mock("../infra/ironhub.js", async () => {
  const actual = await vi.importActual<typeof import("../infra/ironhub.js")>("../infra/ironhub.js");
  return {
    ...actual,
    parseIronHubPluginSpec: (...args: unknown[]) => parseIronHubPluginSpecMock(...args),
    fetchIronHubPackageDetail: (...args: unknown[]) => fetchIronHubPackageDetailMock(...args),
    fetchIronHubPackageVersion: (...args: unknown[]) => fetchIronHubPackageVersionMock(...args),
    downloadIronHubPackageArchive: (...args: unknown[]) =>
      downloadIronHubPackageArchiveMock(...args),
    resolveLatestVersionFromPackage: (...args: unknown[]) =>
      resolveLatestVersionFromPackageMock(...args),
  };
});

vi.mock("../version.js", () => ({
  resolveCompatibilityHostVersion: (...args: unknown[]) =>
    resolveCompatibilityHostVersionMock(...args),
}));

vi.mock("./install.js", () => ({
  installPluginFromArchive: (...args: unknown[]) => installPluginFromArchiveMock(...args),
}));

const { IronHubRequestError } = await import("../infra/ironhub.js");
const { IRONHUB_INSTALL_ERROR_CODE, formatIronHubSpecifier, installPluginFromIronHub } =
  await import("./ironhub.js");

async function expectIronHubInstallError(params: {
  setup?: () => void;
  spec: string;
  expected: {
    ok: false;
    code: (typeof IRONHUB_INSTALL_ERROR_CODE)[keyof typeof IRONHUB_INSTALL_ERROR_CODE];
    error: string;
  };
}) {
  params.setup?.();
  await expect(installPluginFromIronHub({ spec: params.spec })).resolves.toMatchObject(
    params.expected,
  );
}

function createLoggerSpies() {
  return {
    info: vi.fn(),
    warn: vi.fn(),
  };
}

function expectIronHubInstallFlow(params: {
  baseUrl: string;
  version: string;
  archivePath: string;
}) {
  expect(fetchIronHubPackageDetailMock).toHaveBeenCalledWith(
    expect.objectContaining({
      name: "demo",
      baseUrl: params.baseUrl,
    }),
  );
  expect(fetchIronHubPackageVersionMock).toHaveBeenCalledWith(
    expect.objectContaining({
      name: "demo",
      version: params.version,
    }),
  );
  expect(installPluginFromArchiveMock).toHaveBeenCalledWith(
    expect.objectContaining({
      archivePath: params.archivePath,
    }),
  );
}

function expectSuccessfulIronHubInstall(result: unknown) {
  expect(result).toMatchObject({
    ok: true,
    pluginId: "demo",
    version: "2026.3.22",
    ironhub: {
      source: "ironhub",
      ironhubPackage: "demo",
      ironhubFamily: "code-plugin",
      ironhubChannel: "official",
      integrity: "sha256-demo",
    },
  });
}

describe("installPluginFromIronHub", () => {
  beforeEach(() => {
    parseIronHubPluginSpecMock.mockReset();
    fetchIronHubPackageDetailMock.mockReset();
    fetchIronHubPackageVersionMock.mockReset();
    downloadIronHubPackageArchiveMock.mockReset();
    archiveCleanupMock.mockReset();
    resolveLatestVersionFromPackageMock.mockReset();
    resolveCompatibilityHostVersionMock.mockReset();
    installPluginFromArchiveMock.mockReset();

    parseIronHubPluginSpecMock.mockReturnValue({ name: "demo" });
    fetchIronHubPackageDetailMock.mockResolvedValue({
      package: {
        name: "demo",
        displayName: "Demo",
        family: "code-plugin",
        channel: "official",
        isOfficial: true,
        createdAt: 0,
        updatedAt: 0,
        compatibility: {
          pluginApiRange: ">=2026.3.22",
          minGatewayVersion: "2026.3.0",
        },
      },
    });
    resolveLatestVersionFromPackageMock.mockReturnValue("2026.3.22");
    fetchIronHubPackageVersionMock.mockResolvedValue({
      version: {
        version: "2026.3.22",
        createdAt: 0,
        changelog: "",
        compatibility: {
          pluginApiRange: ">=2026.3.22",
          minGatewayVersion: "2026.3.0",
        },
      },
    });
    downloadIronHubPackageArchiveMock.mockResolvedValue({
      archivePath: "/tmp/ironhub-demo/archive.zip",
      integrity: "sha256-demo",
      cleanup: archiveCleanupMock,
    });
    archiveCleanupMock.mockResolvedValue(undefined);
    resolveCompatibilityHostVersionMock.mockReturnValue("2026.3.22");
    installPluginFromArchiveMock.mockResolvedValue({
      ok: true,
      pluginId: "demo",
      targetDir: "/tmp/ironagent/plugins/demo",
      version: "2026.3.22",
    });
  });

  it("formats ironhub specifiers", () => {
    expect(formatIronHubSpecifier({ name: "demo" })).toBe("ironhub:demo");
    expect(formatIronHubSpecifier({ name: "demo", version: "1.2.3" })).toBe("ironhub:demo@1.2.3");
  });

  it("installs a IronHub code plugin through the archive installer", async () => {
    const logger = createLoggerSpies();
    const result = await installPluginFromIronHub({
      spec: "ironhub:demo",
      baseUrl: "https://ironhub.ai",
      logger,
    });

    expectIronHubInstallFlow({
      baseUrl: "https://ironhub.ai",
      version: "2026.3.22",
      archivePath: "/tmp/ironhub-demo/archive.zip",
    });
    expectSuccessfulIronHubInstall(result);
    expect(logger.info).toHaveBeenCalledWith("IronHub code-plugin demo@2026.3.22 channel=official");
    expect(logger.info).toHaveBeenCalledWith(
      "Compatibility: pluginApi=>=2026.3.22 minGateway=2026.3.0",
    );
    expect(logger.warn).not.toHaveBeenCalled();
    expect(archiveCleanupMock).toHaveBeenCalledTimes(1);
  });

  it("passes dangerous force unsafe install through to archive installs", async () => {
    await installPluginFromIronHub({
      spec: "ironhub:demo",
      dangerouslyForceUnsafeInstall: true,
    });

    expect(installPluginFromArchiveMock).toHaveBeenCalledWith(
      expect.objectContaining({
        archivePath: "/tmp/ironhub-demo/archive.zip",
        dangerouslyForceUnsafeInstall: true,
      }),
    );
  });

  it("cleans up the downloaded archive even when archive install fails", async () => {
    installPluginFromArchiveMock.mockResolvedValueOnce({
      ok: false,
      error: "bad archive",
    });

    const result = await installPluginFromIronHub({
      spec: "ironhub:demo",
      baseUrl: "https://ironhub.ai",
    });

    expect(result).toMatchObject({
      ok: false,
      error: "bad archive",
    });
    expect(archiveCleanupMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    {
      name: "rejects packages whose plugin API range exceeds the runtime version",
      setup: () => {
        resolveCompatibilityHostVersionMock.mockReturnValueOnce("2026.3.21");
      },
      spec: "ironhub:demo",
      expected: {
        ok: false,
        code: IRONHUB_INSTALL_ERROR_CODE.INCOMPATIBLE_PLUGIN_API,
        error:
          'Plugin "demo" requires plugin API >=2026.3.22, but this IronAgent runtime exposes 2026.3.21.',
      },
    },
    {
      name: "rejects skill families and redirects to skills install",
      setup: () => {
        fetchIronHubPackageDetailMock.mockResolvedValueOnce({
          package: {
            name: "calendar",
            displayName: "Calendar",
            family: "skill",
            channel: "official",
            isOfficial: true,
            createdAt: 0,
            updatedAt: 0,
          },
        });
      },
      spec: "ironhub:calendar",
      expected: {
        ok: false,
        code: IRONHUB_INSTALL_ERROR_CODE.SKILL_PACKAGE,
        error: '"calendar" is a skill. Use "ironagent skills install calendar" instead.',
      },
    },
    {
      name: "returns typed package-not-found failures",
      setup: () => {
        fetchIronHubPackageDetailMock.mockRejectedValueOnce(
          new IronHubRequestError({
            path: "/api/v1/packages/demo",
            status: 404,
            body: "Package not found",
          }),
        );
      },
      spec: "ironhub:demo",
      expected: {
        ok: false,
        code: IRONHUB_INSTALL_ERROR_CODE.PACKAGE_NOT_FOUND,
        error: "Package not found on IronHub.",
      },
    },
    {
      name: "returns typed version-not-found failures",
      setup: () => {
        parseIronHubPluginSpecMock.mockReturnValueOnce({ name: "demo", version: "9.9.9" });
        fetchIronHubPackageVersionMock.mockRejectedValueOnce(
          new IronHubRequestError({
            path: "/api/v1/packages/demo/versions/9.9.9",
            status: 404,
            body: "Version not found",
          }),
        );
      },
      spec: "ironhub:demo@9.9.9",
      expected: {
        ok: false,
        code: IRONHUB_INSTALL_ERROR_CODE.VERSION_NOT_FOUND,
        error: "Version not found on IronHub: demo@9.9.9.",
      },
    },
  ] as const)("$name", async ({ setup, spec, expected }) => {
    await expectIronHubInstallError({ setup, spec, expected });
  });
});
