import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const officeScriptPath = resolve("skills/office-docs/scripts/make_office_docs.py");
const localPdfScriptPath = resolve("skills/local-pdf/scripts/local_pdf.py");

describe("local-pdf skill script", () => {
  it("inspects a simple pdf and emits offline verification artifacts", () => {
    const workDir = mkdtempSync(join(tmpdir(), "local-pdf-skill-"));
    const sourceDir = join(workDir, "source");
    const outDir = join(workDir, "processed");

    const office = spawnSync(
      "python3",
      [
        officeScriptPath,
        "--out-dir",
        sourceDir,
        "--title",
        "Local PDF Smoke",
        "--body",
        "Generated for offline verification.",
      ],
      { encoding: "utf8" },
    );

    expect(office.status).toBe(0);

    const sourcePdf = join(sourceDir, "agent-word.pdf");
    const manifestJson = join(outDir, "manifest.json");

    const localPdf = spawnSync(
      "python3",
      [
        localPdfScriptPath,
        "--input-pdf",
        sourcePdf,
        "--out-dir",
        outDir,
        "--manifest-json",
        manifestJson,
        "--stdout-json",
      ],
      { encoding: "utf8" },
    );

    expect(localPdf.status).toBe(0);

    const summary = JSON.parse(localPdf.stdout) as {
      pdfVersion: string;
      pageCount: number;
      textOutput: { name: string; path: string; sizeBytes: number };
      copyPdf?: { name: string; path: string; sizeBytes: number };
      summaryPdf?: { name: string; path: string; sizeBytes: number };
      textPreview: string[];
    };

    expect(summary.pdfVersion).toBe("1.4");
    expect(summary.pageCount).toBe(1);
    expect(summary.textOutput.name).toBe("extracted.txt");
    expect(summary.copyPdf?.name).toBe("verified-copy.pdf");
    expect(summary.summaryPdf?.name).toBe("summary.pdf");

    const outFiles = readdirSync(outDir).sort();
    expect(outFiles).toEqual([
      "extracted.txt",
      "manifest.json",
      "summary.pdf",
      "verified-copy.pdf",
    ]);

    const extracted = readFileSync(join(outDir, "extracted.txt"), "utf8");
    expect(extracted).toContain("Local PDF Smoke");
    expect(extracted).toContain("Generated for offline verification.");
  });
});
