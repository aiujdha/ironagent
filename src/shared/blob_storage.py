"""Shared blob storage abstractions and COS-backed implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from urllib.parse import urlparse

from src.shared.config import get_settings
from src.shared.cos_network import (
    configure_cos_sdk_logging,
    cos_config_proxies,
    ensure_cos_no_proxy,
)


@dataclass(slots=True)
class BlobPutResult:
    backend: str
    key: str
    url: str
    etag: str = ""


class BlobStorage(Protocol):
    def is_enabled(self) -> bool: ...

    def put_bytes(self, *, key: str, content: bytes) -> BlobPutResult: ...

    def get_bytes(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


def _cos_settings() -> dict[str, str]:
    settings = get_settings()
    return {
        "secret_id": settings.cos_secret_id.strip(),
        "secret_key": settings.cos_secret_key.strip(),
        "session_token": settings.cos_session_token.strip(),
        "bucket": settings.cos_bucket.strip(),
        "region": settings.cos_region.strip(),
        "endpoint": settings.cos_endpoint.strip(),
        "prefix": settings.cos_prefix.strip().strip("/"),
    }


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
            _cos_endpoint(cos["endpoint"], cos["bucket"], cos["region"]),
            _cos_endpoint("", cos["bucket"], cos["region"]),
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


def is_cos_enabled() -> bool:
    config = _cos_settings()
    return bool(
        config["secret_id"]
        and config["secret_key"]
        and config["bucket"]
        and config["region"]
    )


def _load_cos() -> tuple[Any, Any] | None:
    configure_cos_sdk_logging()
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ModuleNotFoundError:
        return None
    return CosConfig, CosS3Client


def _object_key(key: str, prefix: str) -> str:
    trimmed = key.strip().lstrip("/")
    if not trimmed:
        return prefix or ""
    if prefix:
        return f"{prefix}/{trimmed}"
    return trimmed


def _cos_endpoint(endpoint: str, bucket: str, region: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint:
        return endpoint
    return f"https://{bucket}.cos.{region}.myqcloud.com"


def _cos_config_kwargs(cos: dict[str, str]) -> dict[str, object]:
    parsed = urlparse(cos["endpoint"])
    scheme = parsed.scheme or "https"
    endpoint_host = parsed.netloc or cos["endpoint"]
    kwargs: dict[str, object] = {
        "Region": cos["region"],
        "SecretId": cos["secret_id"],
        "SecretKey": cos["secret_key"],
        "Token": cos["session_token"] or None,
        "Scheme": scheme,
        "Proxies": cos_config_proxies(),
    }
    if endpoint_host and not endpoint_host.startswith(f"{cos['bucket']}."):
        kwargs["Endpoint"] = endpoint_host
    return kwargs


def _build_client() -> Any:
    loaded = _load_cos()
    if loaded is None:
        return None

    CosConfig, CosS3Client = loaded
    cos = _cos_settings()
    ensure_cos_no_proxy(endpoint=cos["endpoint"], bucket=cos["bucket"], region=cos["region"])
    config = CosConfig(**_cos_config_kwargs(cos))
    return CosS3Client(config)


def upload_file_to_cos(local_path: str, key: str) -> tuple[bool, str]:
    if not is_cos_enabled():
        return False, "COS is not configured"

    cos = _cos_settings()
    client = _build_client()
    if client is None:
        return False, "cos-python-sdk-v5 package is not installed"

    try:
        object_key = _object_key(key, cos["prefix"])
        response = client.put_object_from_local_file(
            Bucket=cos["bucket"],
            LocalFilePath=local_path,
            Key=object_key,
        )
        etag = response.get("ETag")
        if etag:
            endpoint = _cos_endpoint(cos["endpoint"], cos["bucket"], cos["region"])
            return True, f"{endpoint}/{object_key}"
        return False, f"Upload failed: {response}"
    except Exception as exc:  # pragma: no cover
        return False, f"{type(exc).__name__}: {exc}"


def delete_file_from_cos(key: str) -> tuple[bool, str]:
    if not is_cos_enabled():
        return False, "COS is not configured"

    cos = _cos_settings()
    client = _build_client()
    if client is None:
        return False, "cos-python-sdk-v5 package is not installed"

    try:
        object_key = _object_key(key, cos["prefix"])
        client.delete_object(Bucket=cos["bucket"], Key=object_key)
        return True, "Deleted"
    except Exception as exc:  # pragma: no cover
        return False, f"{type(exc).__name__}: {exc}"


def download_file_from_cos(key: str) -> tuple[bool, bytes | str]:
    if not is_cos_enabled():
        return False, "COS is not configured"

    cos = _cos_settings()
    client = _build_client()
    if client is None:
        return False, "cos-python-sdk-v5 package is not installed"

    try:
        object_key = _object_key(key, cos["prefix"])
        response = client.get_object(Bucket=cos["bucket"], Key=object_key)
        body = response.get("Body")
        if body is None:
            return False, "Download failed: missing response body"
        return True, body.get_raw_stream().read()
    except Exception as exc:  # pragma: no cover
        return False, f"{type(exc).__name__}: {exc}"


class CosBlobStorage:
    backend = "cos"

    def is_enabled(self) -> bool:
        return is_cos_enabled()

    def put_bytes(self, *, key: str, content: bytes) -> BlobPutResult:
        with NamedTemporaryFile(delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
        try:
            success, result = upload_file_to_cos(str(tmp_path), key)
        finally:
            tmp_path.unlink(missing_ok=True)
        if not success:
            raise ValueError(result)
        return BlobPutResult(backend=self.backend, key=key.strip().lstrip("/"), url=result)

    def get_bytes(self, key: str) -> bytes:
        success, result = download_file_from_cos(key)
        if not success:
            raise ValueError(str(result))
        if isinstance(result, bytes):
            return result
        raise ValueError("unexpected COS download payload")

    def delete(self, key: str) -> None:
        success, result = delete_file_from_cos(key)
        if not success:
            raise ValueError(result)
