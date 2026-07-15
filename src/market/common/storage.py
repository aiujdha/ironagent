from pathlib import Path
from shutil import copy2


def package_storage_dir() -> Path:
    path = Path("./data/packages")
    path.mkdir(parents=True, exist_ok=True)
    return path


class StorageManager:
    """Save and delete marketplace package archives."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def package_dir(self, kind: str, record_id: str) -> Path:
        path = self.base_dir / kind / record_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_package(self, *, kind: str, record_id: str, filename: str, source_path: Path) -> Path:
        target = self.package_dir(kind, record_id) / filename
        copy2(source_path, target)
        return target

    def delete_package(self, *, package_path: Path) -> None:
        if package_path.exists():
            package_path.unlink()
