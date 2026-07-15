"""Thin gateway for optional object storage backends."""

from __future__ import annotations

from pathlib import Path

from src.market.common.cos_client import cos_key_from_url, download_file_from_cos, is_cos_enabled, upload_file_to_cos


class ObjectStorageGateway:
    provider = "cos"

    def is_enabled(self) -> bool:
        return is_cos_enabled()

    def build_key(self, *, assistant_id: str, relative_path: str) -> str:
        trimmed = relative_path.strip().lstrip("/")
        return f"workspace-files/{assistant_id}/{trimmed}"

    def upload_file(self, *, local_path: Path, key: str) -> tuple[bool, str]:
        return upload_file_to_cos(str(local_path), key)

    def resolve_key(self, *, storage_key: str = "", storage_path: str = "") -> str | None:
        if storage_key.strip():
            return storage_key.strip().lstrip("/")
        if storage_path.strip():
            return cos_key_from_url(storage_path)
        return None

    def download_bytes(self, *, key: str) -> tuple[bool, bytes | str, str]:
        return download_file_from_cos(key)
