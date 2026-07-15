import json
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from shutil import copy2, copytree
from typing import Any


@dataclass(slots=True)
class PackageManifest:
    name: str
    version: str
    description: str = ""
    author: str = ""


class PackageHandler:
    """Helpers for reading and unpacking marketplace archives."""

    @staticmethod
    def extract_manifest(package_path: Path, *, kind: str = "skills") -> dict[str, Any]:
        manifest_names = (
            ("ironagent.plugin.json", "manifest.json")
            if kind == "plugins"
            else ("manifest.json", "ironagent.plugin.json")
        )
        suffixes = [suffix.lower() for suffix in package_path.suffixes]
        if zipfile.is_zipfile(package_path):
            with zipfile.ZipFile(package_path) as archive:
                for manifest_name in manifest_names:
                    for name in archive.namelist():
                        if name.endswith(f"/{manifest_name}") or name == manifest_name:
                            return json.loads(archive.read(name).decode("utf-8"))
            return {}

        if suffixes and suffixes[-1] in {".gz", ".tgz", ".tar"}:
            with tarfile.open(package_path, "r:*") as archive:
                members = archive.getmembers()
                for manifest_name in manifest_names:
                    for member in members:
                        if (
                            member.name.endswith(f"/{manifest_name}")
                            or member.name == manifest_name
                        ):
                            extracted = archive.extractfile(member)
                            if extracted is None:
                                continue
                            return json.loads(extracted.read().decode("utf-8"))
            return {}

        return {}

    @staticmethod
    def extract_package(package_path: Path, extract_dir: Path, *, kind: str = "skills") -> Path:
        extract_dir.mkdir(parents=True, exist_ok=True)

        if zipfile.is_zipfile(package_path):
            with zipfile.ZipFile(package_path) as archive:
                PackageHandler._safe_extract_zip(archive, extract_dir)
        else:
            with tarfile.open(package_path, "r:*") as archive:
                PackageHandler._safe_extract_tar(archive, extract_dir)

        return PackageHandler.resolve_package_root(extract_dir, kind=kind)

    @staticmethod
    def resolve_package_root(extract_dir: Path, *, kind: str = "skills") -> Path:
        marker_names = (
            {"ironagent.plugin.json"}
            if kind == "plugins"
            else {"manifest.json", "SKILL.md"}
        )
        if PackageHandler._contains_package_marker(extract_dir, marker_names):
            return extract_dir

        entries = [item for item in extract_dir.iterdir() if item.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir():
            nested = entries[0]
            if PackageHandler._contains_package_marker(nested, marker_names):
                return nested

        candidates: list[Path] = []
        for candidate in extract_dir.rglob("*"):
            if not candidate.is_dir() or "__MACOSX" in candidate.parts:
                continue
            if PackageHandler._contains_package_marker(candidate, marker_names):
                candidates.append(candidate)
        if candidates:
            candidates.sort(key=lambda path: (len(path.relative_to(extract_dir).parts), str(path)))
            shallowest_depth = len(candidates[0].relative_to(extract_dir).parts)
            shallowest = [
                candidate
                for candidate in candidates
                if len(candidate.relative_to(extract_dir).parts) == shallowest_depth
            ]
            if len(shallowest) == 1:
                return shallowest[0]
        return extract_dir

    @staticmethod
    def _contains_package_marker(package_dir: Path, marker_names: set[str]) -> bool:
        return any((package_dir / name).is_file() for name in marker_names)

    @staticmethod
    def materialize_package_contents(package_root: Path, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        for entry in package_root.iterdir():
            destination = target_dir / entry.name
            if entry.is_dir():
                copytree(entry, destination, dirs_exist_ok=True, copy_function=copy2)
                continue
            copy2(entry, destination)

    @staticmethod
    def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
        target_root = target_dir.resolve()
        for member in archive.infolist():
            member_path = (target_root / member.filename).resolve()
            if not str(member_path).startswith(str(target_root)):
                raise ValueError("zip archive contains an unsafe path")
        archive.extractall(target_root)

    @staticmethod
    def _safe_extract_tar(archive: tarfile.TarFile, target_dir: Path) -> None:
        target_root = target_dir.resolve()
        for member in archive.getmembers():
            member_path = (target_root / member.name).resolve()
            if not str(member_path).startswith(str(target_root)):
                raise ValueError("tar archive contains an unsafe path")
        archive.extractall(target_root)
