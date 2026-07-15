#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from io import BytesIO


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect and locally process a PDF without external model dependencies."
    )
    parser.add_argument("--input-pdf", required=True, help="Input PDF path.")
    parser.add_argument("--out-dir", required=True, help="Output directory.")
    parser.add_argument("--text-output", default="extracted.txt", help="Extracted text filename.")
    parser.add_argument("--copy-pdf", default="verified-copy.pdf", help="Verified copy PDF filename.")
    parser.add_argument("--summary-pdf", default="summary.pdf", help="Summary PDF filename.")
    parser.add_argument(
        "--manifest-json",
        help="Optional JSON file path to write a machine-readable summary.",
    )
    parser.add_argument(
        "--stdout-json",
        action="store_true",
        help="Print the summary as JSON to stdout.",
    )
    parser.add_argument(
        "--no-copy-pdf",
        action="store_true",
        help="Skip writing the verified copy PDF.",
    )
    parser.add_argument(
        "--no-summary-pdf",
        action="store_true",
        help="Skip writing the generated summary PDF.",
    )
    return parser.parse_args()


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def extract_literal_pdf_strings(data: bytes) -> list[str]:
    pattern = re.compile(rb"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj")
    values: list[str] = []
    for match in pattern.finditer(data):
        raw = match.group(1)
        decoded = (
            raw.replace(b"\\(", b"(")
            .replace(b"\\)", b")")
            .replace(b"\\\\", b"\\")
            .decode("utf-8", errors="replace")
        )
        if decoded.strip():
            values.append(decoded)
    return values


def count_pdf_pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", data))


def build_min_pdf(path: str, lines: list[str]) -> None:
    buffer = BytesIO()

    def add_object(obj_str: str, offsets: list[int]) -> None:
        offsets.append(buffer.tell())
        buffer.write((obj_str + "\n").encode("ascii"))

    buffer.write(b"%PDF-1.4\n")

    offsets: list[int] = []
    add_object("1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj", offsets)
    add_object("2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj", offsets)
    add_object(
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj",
        offsets,
    )

    text_lines = ["BT /F1 12 Tf 72 730 Td"]
    first = True
    for line in lines:
        safe = escape_pdf_text(line)
        if first:
            text_lines.append(f"({safe}) Tj")
            first = False
        else:
            text_lines.append("T*")
            text_lines.append(f"({safe}) Tj")
    text_lines.append("ET")

    stream_data = ("\n".join(text_lines) + "\n").encode("ascii", errors="replace")
    stream_header = f"4 0 obj << /Length {len(stream_data)} >> stream\n".encode("ascii")
    stream_footer = b"endstream\nendobj\n"
    contents_offset = buffer.tell()
    buffer.write(stream_header)
    buffer.write(stream_data)
    buffer.write(stream_footer)

    add_object("5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj", offsets)

    xref_offset = buffer.tell()
    buffer.write(b"xref\n0 6\n")
    buffer.write(b"0000000000 65535 f \n")
    object_offsets = [offsets[0], offsets[1], offsets[2], contents_offset, offsets[3]]
    for offset in object_offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode("ascii"))

    buffer.write(b"trailer << /Size 6 /Root 1 0 R >>\n")
    buffer.write(b"startxref\n")
    buffer.write(str(xref_offset).encode("ascii"))
    buffer.write(b"\n%%EOF\n")

    ensure_parent(path)
    with open(path, "wb") as f:
        f.write(buffer.getvalue())


def main() -> int:
    args = parse_args()

    input_pdf = os.path.abspath(args.input_pdf)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    with open(input_pdf, "rb") as f:
        data = f.read()

    if not data.startswith(b"%PDF-"):
        raise SystemExit(f"Input is not a PDF: {input_pdf}")

    pdf_version = data[5:8].decode("ascii", errors="replace").strip()
    page_count = count_pdf_pages(data)
    extracted_lines = extract_literal_pdf_strings(data)
    extracted_text = "\n".join(extracted_lines).strip()

    text_output = os.path.join(out_dir, args.text_output)
    ensure_parent(text_output)
    with open(text_output, "w", encoding="utf-8") as f:
        f.write(extracted_text + ("\n" if extracted_text else ""))

    copy_pdf_path = None
    if not args.no_copy_pdf:
        copy_pdf_path = os.path.join(out_dir, args.copy_pdf)
        ensure_parent(copy_pdf_path)
        shutil.copyfile(input_pdf, copy_pdf_path)

    summary_pdf_path = None
    if not args.no_summary_pdf:
        summary_pdf_path = os.path.join(out_dir, args.summary_pdf)
        preview = extracted_lines[:4] if extracted_lines else ["No inline text extracted."]
        build_min_pdf(
            summary_pdf_path,
            [
                "Local PDF Summary",
                f"Source: {os.path.basename(input_pdf)}",
                f"PDF version: {pdf_version}",
                f"Pages: {page_count}",
                *preview,
            ],
        )

    summary: dict[str, object] = {
        "inputPdf": input_pdf,
        "outDir": out_dir,
        "pdfVersion": pdf_version,
        "pageCount": page_count,
        "sizeBytes": len(data),
        "textOutput": {
            "name": os.path.basename(text_output),
            "path": text_output,
            "sizeBytes": os.path.getsize(text_output),
        },
        "textPreview": extracted_lines[:8],
    }

    if copy_pdf_path:
        summary["copyPdf"] = {
            "name": os.path.basename(copy_pdf_path),
            "path": copy_pdf_path,
            "sizeBytes": os.path.getsize(copy_pdf_path),
        }

    if summary_pdf_path:
        summary["summaryPdf"] = {
            "name": os.path.basename(summary_pdf_path),
            "path": summary_pdf_path,
            "sizeBytes": os.path.getsize(summary_pdf_path),
        }

    if args.manifest_json:
        manifest_path = os.path.abspath(args.manifest_json)
        ensure_parent(manifest_path)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")

    if args.stdout_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(text_output)
        if copy_pdf_path:
            print(copy_pdf_path)
        if summary_pdf_path:
            print(summary_pdf_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
