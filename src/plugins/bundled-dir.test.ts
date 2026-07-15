import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resolveBundledPluginsDir } from "./bundled-dir.js";

const tempDirs: string[] = [];
const originalBundledDir = process.env.IRONAGENT_BUNDLED_PLUGINS_DIR;
const originalVitest = process.env.VITEST;
const originalArgv1 = process.argv[1];

function makeRepoRoot(prefix: string): string {
  const repoRoot = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  tempDirs.push(repoRoot);
  return repoRoot;
}

function createIronAgentRoot(params: {
  prefix: string;
  hasExtensions?: boolean;
  hasSrc?: boolean;
  hasDistRuntimeExtensions?: boolean;
  hasDistExtensions?: boolean;
  hasGitCheckout?: boolean;
}) {
  const repoRoot = makeRepoRoot(params.prefix);
  if (params.hasExtensions) {
    fs.mkdirSync(path.join(repoRoot, "extensions"), { recursive: true });
  }
  if (params.hasSrc) {
    fs.mkdirSync(path.join(repoRoot, "src"), { recursive: true });
  }
  if (params.hasDistRuntimeExtensions) {
    fs.mkdirSync(path.join(repoRoot, "dist-runtime", "extensions"), { recursive: true });
  }
  if (params.hasDistExtensions) {
    fs.mkdirSync(path.join(repoRoot, "dist", "extensions"), { recursive: true });
  }
  if (params.hasGitCheckout) {
    fs.writeFileSync(path.join(repoRoot, ".git"), "gitdir: /tmp/fake.git\n", "utf8");
  }
  fs.writeFileSync(
    path.join(repoRoot, "package.json"),
    `${JSON.stringify({ name: "ironagent" }, null, 2)}\n`,
    "utf8",
  );
  return repoRoot;
}

function expectResolvedBundledDir(params: {
  cwd: string;
  expectedDir: string;
  argv1?: string;
  bundledDirOverride?: string;
  vitest?: string;
}) {
  vi.spyOn(process, "cwd").mockReturnValue(params.cwd);
  process.argv[1] = params.argv1 ?? "/usr/bin/env";
  if (params.vitest === undefined) {
    delete process.env.VITEST;
  } else {
    process.env.VITEST = params.vitest;
  }
  if (params.bundledDirOverride === undefined) {
    delete process.env.IRONAGENT_BUNDLED_PLUGINS_DIR;
  } else {
    process.env.IRONAGENT_BUNDLED_PLUGINS_DIR = params.bundledDirOverride;
  }

  expect(fs.realpathSync(resolveBundledPluginsDir() ?? "")).toBe(
    fs.realpathSync(params.expectedDir),
  );
}

function expectResolvedBundledDirFromRoot(params: {
  repoRoot: string;
  expectedRelativeDir: string;
  argv1?: string;
  bundledDirOverride?: string;
  vitest?: string;
  cwd?: string;
}) {
  expectResolvedBundledDir({
    cwd: params.cwd ?? params.repoRoot,
    expectedDir: path.join(params.repoRoot, params.expectedRelativeDir),
    ...(params.argv1 ? { argv1: params.argv1 } : {}),
    ...(params.bundledDirOverride ? { bundledDirOverride: params.bundledDirOverride } : {}),
    ...(params.vitest !== undefined ? { vitest: params.vitest } : {}),
  });
}

function expectInstalledBundledDirScenario(params: {
  installedRoot: string;
  cwd?: string;
  argv1?: string;
  bundledDirOverride?: string;
}) {
  expectResolvedBundledDirFromRoot({
    repoRoot: params.installedRoot,
    cwd: params.cwd ?? process.cwd(),
    ...(params.argv1 ? { argv1: params.argv1 } : {}),
    ...(params.bundledDirOverride ? { bundledDirOverride: params.bundledDirOverride } : {}),
    expectedRelativeDir: path.join("dist", "extensions"),
  });
}

function expectInstalledBundledDirScenarioCase(
  createScenario: () => {
    installedRoot: string;
    cwd?: string;
    argv1?: string;
    bundledDirOverride?: string;
  },
) {
  expectInstalledBundledDirScenario(createScenario());
}

afterEach(() => {
  vi.restoreAllMocks();
  if (originalBundledDir === undefined) {
    delete process.env.IRONAGENT_BUNDLED_PLUGINS_DIR;
  } else {
    process.env.IRONAGENT_BUNDLED_PLUGINS_DIR = originalBundledDir;
  }
  if (originalVitest === undefined) {
    delete process.env.VITEST;
  } else {
    process.env.VITEST = originalVitest;
  }
  process.argv[1] = originalArgv1;
  for (const dir of tempDirs.splice(0, tempDirs.length)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

describe("resolveBundledPluginsDir", () => {
  it.each([
    [
      "prefers the staged runtime bundled plugin tree from the package root",
      {
        prefix: "ironagent-bundled-dir-runtime-",
        hasDistRuntimeExtensions: true,
        hasDistExtensions: true,
      },
      {
        expectedRelativeDir: path.join("dist-runtime", "extensions"),
      },
    ],
    [
      "falls back to built dist/extensions in installed package roots",
      {
        prefix: "ironagent-bundled-dir-dist-",
        hasDistExtensions: true,
      },
      {
        expectedRelativeDir: path.join("dist", "extensions"),
      },
    ],
    [
      "prefers source extensions under vitest to avoid stale staged plugins",
      {
        prefix: "ironagent-bundled-dir-vitest-",
        hasExtensions: true,
        hasDistRuntimeExtensions: true,
        hasDistExtensions: true,
      },
      {
        expectedRelativeDir: "extensions",
        vitest: "true",
      },
    ],
    [
      "prefers source extensions in a git checkout even without vitest env",
      {
        prefix: "ironagent-bundled-dir-git-",
        hasExtensions: true,
        hasSrc: true,
        hasDistRuntimeExtensions: true,
        hasDistExtensions: true,
        hasGitCheckout: true,
      },
      {
        expectedRelativeDir: "extensions",
      },
    ],
  ] as const)("%s", (_name, layout, expectation) => {
    const repoRoot = createIronAgentRoot(layout);
    expectResolvedBundledDirFromRoot({
      repoRoot,
      expectedRelativeDir: expectation.expectedRelativeDir,
      ...("vitest" in expectation ? { vitest: expectation.vitest } : {}),
    });
  });

  it.each([
    {
      name: "prefers the running CLI package root over an unrelated cwd checkout",
      createScenario: () => {
        const installedRoot = createIronAgentRoot({
          prefix: "ironagent-bundled-dir-installed-",
          hasDistExtensions: true,
        });
        const cwdRepoRoot = createIronAgentRoot({
          prefix: "ironagent-bundled-dir-cwd-",
          hasExtensions: true,
          hasSrc: true,
          hasGitCheckout: true,
        });
        return {
          installedRoot,
          cwd: cwdRepoRoot,
          argv1: path.join(installedRoot, "ironagent.mjs"),
        };
      },
    },
    {
      name: "falls back to the running installed package when the override path is stale",
      createScenario: () => {
        const installedRoot = createIronAgentRoot({
          prefix: "ironagent-bundled-dir-override-",
          hasDistExtensions: true,
        });
        return {
          installedRoot,
          argv1: path.join(installedRoot, "ironagent.mjs"),
          bundledDirOverride: path.join(installedRoot, "missing-extensions"),
        };
      },
    },
  ] as const)("$name", ({ createScenario }) => {
    expectInstalledBundledDirScenarioCase(createScenario);
  });
});
