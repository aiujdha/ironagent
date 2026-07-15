---
name: local-pdf
description: Inspect and locally process a PDF without external model dependencies. Use when the user wants an offline PDF smoke test, basic text extraction from simple PDFs, a verified copy, or a summary artifact inside the workspace.
metadata: { "ironagent": { "emoji": "📄", "requires": { "bins": ["python3"] } } }
---

# Local PDF

Use the bundled script to inspect an existing PDF and create local verification artifacts without calling any external model or service.

This skill is intended for offline smoke tests and deterministic workspace validation. It is especially useful when you want to verify that IronAgent can find, read, and process a PDF inside the container without relying on internet connectivity or third-party APIs.

## When to use

- The user asks for an offline PDF handling smoke test.
- You need to extract text from a simple PDF and save it into the workspace.
- You need a verified copy of a PDF plus a machine-readable summary.
- You want a local PDF processing step that does not depend on Gemini, OpenAI, or any other external model.

## Workflow

1. Pick an existing PDF inside the active workspace.
2. Run the bundled script with an output directory inside the workspace.
3. Prefer `--stdout-json` for smoke runs so you get a structured summary immediately.
4. Verify the generated `.txt`, `.json`, `.pdf`, and copied PDF files exist before replying.

## Quick start

```bash
python3 {baseDir}/scripts/local_pdf.py \
  --input-pdf ./temp/reverify-office-20260407/agent-word.pdf \
  --out-dir ./temp/local-pdf-smoke \
  --stdout-json
```

Default outputs:

- Extracted text: `extracted.txt`
- Verified copy: `verified-copy.pdf`
- Summary PDF: `summary.pdf`

## Common variants

Custom output names:

```bash
python3 {baseDir}/scripts/local_pdf.py \
  --input-pdf ./temp/invoice.pdf \
  --out-dir ./temp/invoice-review \
  --text-output invoice.txt \
  --copy-pdf invoice-copy.pdf \
  --summary-pdf invoice-summary.pdf \
  --manifest-json ./temp/invoice-review/manifest.json
```

Text-only extraction:

```bash
python3 {baseDir}/scripts/local_pdf.py \
  --input-pdf ./temp/report.pdf \
  --out-dir ./temp/report-text \
  --no-copy-pdf \
  --no-summary-pdf \
  --stdout-json
```

## Practical guidance

- Keep all outputs inside the current workspace unless the user explicitly asks otherwise.
- This skill is best for deterministic offline validation, not AI-powered semantic PDF editing.
- For smoke tests, prefer `--stdout-json` or `--manifest-json` so the final reply can quote exact output paths and sizes.
- The text extraction is intentionally lightweight and works best on simple or uncompressed PDFs. For complex PDFs, use the JSON summary and verified copy as the validation artifact.
