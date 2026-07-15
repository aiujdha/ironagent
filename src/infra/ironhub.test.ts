import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  downloadIronHubPackageArchive,
  downloadIronHubSkillArchive,
  parseIronHubPluginSpec,
  resolveIronHubAuthToken,
  resolveLatestVersionFromPackage,
  satisfiesGatewayMinimum,
  satisfiesPluginApiRange,
  searchIronHubSkills,
} from "./ironhub.js";

describe("ironhub helpers", () => {
  const originalHome = process.env.HOME;

  afterEach(() => {
    delete process.env.IRONAGENT_IRONHUB_TOKEN;
    delete process.env.IRONHUB_TOKEN;
    delete process.env.IRONHUB_AUTH_TOKEN;
    delete process.env.IRONAGENT_IRONHUB_CONFIG_PATH;
    delete process.env.IRONHUB_CONFIG_PATH;
    delete process.env.XDG_CONFIG_HOME;
    if (originalHome == null) {
      delete process.env.HOME;
    } else {
      process.env.HOME = originalHome;
    }
  });

  it("parses explicit IronHub package specs", () => {
    expect(parseIronHubPluginSpec("ironhub:demo")).toEqual({
      name: "demo",
    });
    expect(parseIronHubPluginSpec("ironhub:demo@1.2.3")).toEqual({
      name: "demo",
      version: "1.2.3",
    });
    expect(parseIronHubPluginSpec("@scope/pkg")).toBeNull();
  });

  it("resolves latest versions from latestVersion before tags", () => {
    expect(
      resolveLatestVersionFromPackage({
        package: {
          name: "demo",
          displayName: "Demo",
          family: "code-plugin",
          channel: "official",
          isOfficial: true,
          createdAt: 0,
          updatedAt: 0,
          latestVersion: "1.2.3",
          tags: { latest: "1.2.2" },
        },
      }),
    ).toBe("1.2.3");
    expect(
      resolveLatestVersionFromPackage({
        package: {
          name: "demo",
          displayName: "Demo",
          family: "code-plugin",
          channel: "official",
          isOfficial: true,
          createdAt: 0,
          updatedAt: 0,
          tags: { latest: "1.2.2" },
        },
      }),
    ).toBe("1.2.2");
  });

  it("checks plugin api ranges without semver dependency", () => {
    expect(satisfiesPluginApiRange("1.2.3", "^1.2.0")).toBe(true);
    expect(satisfiesPluginApiRange("1.9.0", ">=1.2.0 <2.0.0")).toBe(true);
    expect(satisfiesPluginApiRange("2.0.0", "^1.2.0")).toBe(false);
    expect(satisfiesPluginApiRange("1.1.9", ">=1.2.0")).toBe(false);
    expect(satisfiesPluginApiRange("2026.3.22", ">=2026.3.22")).toBe(true);
    expect(satisfiesPluginApiRange("2026.3.21", ">=2026.3.22")).toBe(false);
    expect(satisfiesPluginApiRange("invalid", "^1.2.0")).toBe(false);
  });

  it("checks min gateway versions with loose host labels", () => {
    expect(satisfiesGatewayMinimum("2026.3.22", "2026.3.0")).toBe(true);
    expect(satisfiesGatewayMinimum("IronAgent 2026.3.22", "2026.3.0")).toBe(true);
    expect(satisfiesGatewayMinimum("2026.2.9", "2026.3.0")).toBe(false);
    expect(satisfiesGatewayMinimum("unknown", "2026.3.0")).toBe(false);
  });

  it("resolves IronHub auth token from config.json", async () => {
    const configRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ironagent-ironhub-config-"));
    const configPath = path.join(configRoot, "ironhub", "config.json");
    process.env.IRONAGENT_IRONHUB_CONFIG_PATH = configPath;
    await fs.mkdir(path.dirname(configPath), { recursive: true });
    await fs.writeFile(configPath, JSON.stringify({ auth: { token: "cfg-token-123" } }), "utf8");

    await expect(resolveIronHubAuthToken()).resolves.toBe("cfg-token-123");
  });

  it.runIf(process.platform === "darwin")(
    "resolves IronHub auth token from the macOS Application Support path",
    async () => {
      const fakeHome = await fs.mkdtemp(path.join(os.tmpdir(), "ironagent-ironhub-home-"));
      const configPath = path.join(
        fakeHome,
        "Library",
        "Application Support",
        "ironhub",
        "config.json",
      );
      const homedirSpy = vi.spyOn(os, "homedir").mockReturnValue(fakeHome);
      try {
        await fs.mkdir(path.dirname(configPath), { recursive: true });
        await fs.writeFile(configPath, JSON.stringify({ token: "macos-token-123" }), "utf8");

        await expect(resolveIronHubAuthToken()).resolves.toBe("macos-token-123");
      } finally {
        homedirSpy.mockRestore();
      }
    },
  );

  it.runIf(process.platform === "darwin")(
    "falls back to XDG_CONFIG_HOME on macOS when Application Support has no config",
    async () => {
      const fakeHome = await fs.mkdtemp(path.join(os.tmpdir(), "ironagent-ironhub-home-"));
      const xdgRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ironagent-ironhub-xdg-"));
      const configPath = path.join(xdgRoot, "ironhub", "config.json");
      const homedirSpy = vi.spyOn(os, "homedir").mockReturnValue(fakeHome);
      process.env.XDG_CONFIG_HOME = xdgRoot;
      try {
        await fs.mkdir(path.dirname(configPath), { recursive: true });
        await fs.writeFile(configPath, JSON.stringify({ token: "xdg-token-123" }), "utf8");

        await expect(resolveIronHubAuthToken()).resolves.toBe("xdg-token-123");
      } finally {
        homedirSpy.mockRestore();
      }
    },
  );

  it("injects resolved auth token into IronHub requests", async () => {
    process.env.IRONAGENT_IRONHUB_TOKEN = "env-token-123";
    const fetchImpl = async (input: string | URL | Request, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      expect(url).toContain("/api/v1/search");
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer env-token-123");
      return new Response(JSON.stringify({ results: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    };

    await expect(searchIronHubSkills({ query: "calendar", fetchImpl })).resolves.toEqual([]);
  });
  it("downloads package archives to sanitized temp paths and cleans them up", async () => {
    const archive = await downloadIronHubPackageArchive({
      name: "@hyf/zai-external-alpha",
      version: "0.0.1",
      fetchImpl: async () =>
        new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: { "content-type": "application/zip" },
        }),
    });

    try {
      expect(path.basename(archive.archivePath)).toBe("zai-external-alpha.zip");
      expect(archive.archivePath.includes("@hyf")).toBe(false);
      await expect(fs.readFile(archive.archivePath)).resolves.toEqual(Buffer.from([1, 2, 3]));
    } finally {
      const archiveDir = path.dirname(archive.archivePath);
      await archive.cleanup();
      await expect(fs.stat(archiveDir)).rejects.toThrow();
    }
  });

  it("downloads skill archives to sanitized temp paths and cleans them up", async () => {
    const archive = await downloadIronHubSkillArchive({
      slug: "agentreceipt",
      version: "1.0.0",
      fetchImpl: async () =>
        new Response(new Uint8Array([4, 5, 6]), {
          status: 200,
          headers: { "content-type": "application/zip" },
        }),
    });

    try {
      expect(path.basename(archive.archivePath)).toBe("agentreceipt.zip");
      await expect(fs.readFile(archive.archivePath)).resolves.toEqual(Buffer.from([4, 5, 6]));
    } finally {
      const archiveDir = path.dirname(archive.archivePath);
      await archive.cleanup();
      await expect(fs.stat(archiveDir)).rejects.toThrow();
    }
  });
});
