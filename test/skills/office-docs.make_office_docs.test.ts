import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const scriptPath = resolve("skills/office-docs/scripts/make_office_docs.py");

describe("office-docs make_office_docs.py", () => {
  it("writes office artifacts and emits a JSON summary", async () => {
    const workDir = mkdtempSync(join(tmpdir(), "office-docs-skill-"));
    const outDir = join(workDir, "artifacts");
    const manifestPath = join(outDir, "office-manifest.json");

    try {
      const result = spawnSync(
        "python3",
        [
          scriptPath,
          "--out-dir",
          outDir,
          "--title",
          "Smoke Title",
          "--body",
          "Generated in test.",
          "--sheet-name",
          "Summary",
          "--headers",
          "Item,Status",
          "--row",
          "Office Smoke,Done",
          "--manifest-json",
          manifestPath,
          "--stdout-json",
        ],
        { encoding: "utf8" },
      );

      expect(result.status).toBe(0);
      expect(result.stderr).toBe("");

      const summary = JSON.parse(result.stdout) as {
        outDir: string;
        files: Array<{ name: string; path: string; sizeBytes: number }>;
        word: {
          docx: { name: string; path: string; sizeBytes: number };
          pdf: { name: string; path: string; sizeBytes: number };
        };
        sheet: {
          xlsx: { name: string; path: string; sizeBytes: number };
          pdf: { name: string; path: string; sizeBytes: number };
        };
      };

      expect(summary.outDir).toBe(outDir);
      expect(summary.files.map((file) => file.name)).toEqual([
        "agent-word.docx",
        "agent-word.pdf",
        "agent-sheet.xlsx",
        "agent-sheet.pdf",
      ]);
      for (const file of summary.files) {
        expect(statSync(file.path).isFile()).toBe(true);
        expect(file.sizeBytes).toBeGreaterThan(0);
      }
      expect(summary.word.docx.name).toBe("agent-word.docx");
      expect(summary.word.pdf.name).toBe("agent-word.pdf");
      expect(summary.sheet.xlsx.name).toBe("agent-sheet.xlsx");
      expect(summary.sheet.pdf.name).toBe("agent-sheet.pdf");

      const manifestSummary = JSON.parse(readFileSync(manifestPath, "utf8"));
      expect(manifestSummary).toEqual(summary);
    } finally {
      rmSync(workDir, { recursive: true, force: true });
    }
  });
});
