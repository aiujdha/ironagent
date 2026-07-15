#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

ALLOWED_SPEC_STATUS = {"draft", "ready", "blocked", "implemented", "verified"}
VALID_LEVELS = {"S1", "S2", "S3"}
B_ID_PATTERN = re.compile(r"\bB(\d+)\b")


@dataclass(slots=True)
class Finding:
    severity: str
    path: Path
    message: str


@dataclass(slots=True)
class Metadata:
    module: str = ""
    level: str = ""
    status: str = ""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_metadata(text: str) -> Metadata:
    metadata = Metadata()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        match = re.match(r"^-\s*([^:：]+)\s*[:：]\s*(.*)$", stripped)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key in {"模块", "module"}:
            metadata.module = value
        elif key in {"等级", "level", "severity"}:
            metadata.level = value.upper()
        elif key in {"状态", "status"}:
            metadata.status = value.lower()
    return metadata


def b_ids(text: str) -> set[str]:
    return {f"B{match.group(1)}" for match in B_ID_PATTERN.finditer(text)}


def has_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns)


def check_metadata(path: Path, findings: list[Finding]) -> Metadata:
    metadata = parse_metadata(read_text(path))
    if not metadata.module:
        findings.append(Finding("ERROR", path, "missing module metadata"))
    if not metadata.level:
        findings.append(Finding("ERROR", path, "missing level metadata"))
    elif metadata.level not in VALID_LEVELS:
        findings.append(
            Finding("ERROR", path, f"invalid level {metadata.level!r}; expected S1/S2/S3")
        )
    if not metadata.status:
        findings.append(Finding("ERROR", path, "missing status metadata"))
    elif metadata.status not in ALLOWED_SPEC_STATUS:
        allowed = ", ".join(sorted(ALLOWED_SPEC_STATUS))
        findings.append(
            Finding("ERROR", path, f"invalid status {metadata.status!r}; expected one of {allowed}")
        )
    return metadata


def check_test_report(path: Path, findings: list[Finding]) -> None:
    text = read_text(path)
    checks = [
        (
            "branch",
            [r"\bBranch\b", r"验收分支", r"分支"],
        ),
        (
            "HEAD or commit",
            [r"\bHEAD\b", r"\bcommit\b", r"验收目标 commit", r"基线 commit"],
        ),
        (
            "result/conclusion",
            [r"\bResult\b", r"结论"],
        ),
        (
            "acceptance matrix",
            [r"Acceptance Matrix", r"验收矩阵", r"行为覆盖矩阵"],
        ),
    ]
    for label, patterns in checks:
        if not has_any(text, patterns):
            findings.append(Finding("WARN", path, f"TEST_REPORT missing {label} field/heading"))


def check_spec_dir(spec_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    product_path = spec_dir / "PRODUCT.md"
    tech_path = spec_dir / "TECH.md"
    report_path = spec_dir / "TEST_REPORT.md"

    if not product_path.exists():
        findings.append(Finding("ERROR", product_path, "missing PRODUCT.md"))
        return findings
    if not tech_path.exists():
        findings.append(Finding("ERROR", tech_path, "missing TECH.md"))
        return findings

    product_metadata = check_metadata(product_path, findings)
    tech_metadata = check_metadata(tech_path, findings)

    levels = {product_metadata.level, tech_metadata.level}
    if levels & {"S2", "S3"} and not report_path.exists():
        findings.append(Finding("ERROR", report_path, "S2/S3 spec missing TEST_REPORT.md"))

    if report_path.exists():
        check_test_report(report_path, findings)
        product_b_ids = b_ids(read_text(product_path))
        report_b_ids = b_ids(read_text(report_path))
        missing = sorted(product_b_ids - report_b_ids, key=lambda value: int(value[1:]))
        if missing:
            preview = ", ".join(missing[:12])
            suffix = "" if len(missing) <= 12 else f", ... ({len(missing)} total)"
            findings.append(
                Finding(
                    "WARN",
                    report_path,
                    f"PRODUCT B ids not found in TEST_REPORT: {preview}{suffix}",
            )
                )

    return findings


def iter_spec_dirs(specs_root: Path) -> list[Path]:
    return sorted(
        path
        for path in specs_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )


def format_finding(root: Path, finding: Finding) -> str:
    try:
        display_path = finding.path.relative_to(root)
    except ValueError:
        display_path = finding.path
    return f"[{finding.severity}] {display_path}: {finding.message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minimal spec/report lint.")
    parser.add_argument("--specs-root", type=Path, default=Path("specs"))
    parser.add_argument("--strict", action="store_true", help="Exit 1 when ERROR findings exist.")
    args = parser.parse_args(argv)

    root = Path.cwd()
    specs_root = args.specs_root
    if not specs_root.exists():
        print(f"[ERROR] {specs_root}: specs root does not exist")
        return 1 if args.strict else 0

    findings: list[Finding] = []
    spec_dirs = iter_spec_dirs(specs_root)
    for spec_dir in spec_dirs:
        findings.extend(check_spec_dir(spec_dir))

    error_count = sum(1 for finding in findings if finding.severity == "ERROR")
    warning_count = sum(1 for finding in findings if finding.severity == "WARN")

    print(
        f"spec-lint checked={len(spec_dirs)} errors={error_count} warnings={warning_count}"
    )
    for finding in findings:
        print(format_finding(root, finding))

    if args.strict and error_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
