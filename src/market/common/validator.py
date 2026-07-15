from pathlib import Path
from typing import Any

from src.market.common.package import PackageManifest


def validate_manifest(manifest: PackageManifest) -> bool:
    return bool(manifest.name and manifest.version)


class PackageValidator:
    """Lightweight structural validation for marketplace packages."""

    @staticmethod
    def validate_manifest_content(manifest: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for field in ("name", "version"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"Missing required field: {field}")
        return errors

    @staticmethod
    def validate_package_structure(package_dir: Path, *, kind: str = "skills") -> list[str]:
        errors: list[str] = []
        manifest_path = package_dir / "manifest.json"
        skill_md_path = package_dir / "SKILL.md"
        plugin_manifest_path = package_dir / "ironagent.plugin.json"
        if kind == "plugins":
            if not plugin_manifest_path.exists():
                errors.append("ironagent.plugin.json not found")
            return errors
        if not manifest_path.exists() and not skill_md_path.exists():
            errors.append("manifest.json or SKILL.md not found")
        return errors
