"""Backward-compatible COS helpers used by market uploads."""

from __future__ import annotations

from urllib.parse import urlparse

from src.shared import blob_storage as shared_blob_storage


def _cos_settings() -> dict[str, str]:
    return shared_blob_storage._cos_settings()


def cos_key_from_url(path: str) -> str | None:
    trimmed = path.strip()
    if not trimmed:
        return None

    cos = _cos_settings()
    parsed = urlparse(trimmed)
    if parsed.scheme and parsed.netloc:
        key = parsed.path.lstrip("/")
        if not key:
            return None

        endpoints = {
            shared_blob_storage._cos_endpoint(
                cos["endpoint"],
                cos["bucket"],
                cos["region"],
            ),
            shared_blob_storage._cos_endpoint("", cos["bucket"], cos["region"]),
        }
        parsed_endpoints = {
            (candidate.scheme, candidate.netloc)
            for candidate in (urlparse(endpoint) for endpoint in endpoints)
            if candidate.netloc
        }
        if (parsed.scheme, parsed.netloc) in parsed_endpoints:
            prefix = cos["prefix"].strip("/")
            if prefix and key.startswith(f"{prefix}/"):
                return key[len(prefix) + 1 :] or None
            return key or None
        return key

    return trimmed.lstrip("/") or None


def delete_file_from_cos(key: str) -> tuple[bool, str]:
    return shared_blob_storage.delete_file_from_cos(key)


def download_file_from_cos(key: str) -> tuple[bool, bytes | str]:
    return shared_blob_storage.download_file_from_cos(key)


def is_cos_enabled() -> bool:
    return shared_blob_storage.is_cos_enabled()


def upload_file_to_cos(local_path: str, key: str) -> tuple[bool, str]:
    return shared_blob_storage.upload_file_to_cos(local_path, key)


__all__ = [
    "_cos_settings",
    "cos_key_from_url",
    "delete_file_from_cos",
    "download_file_from_cos",
    "is_cos_enabled",
    "upload_file_to_cos",
]
