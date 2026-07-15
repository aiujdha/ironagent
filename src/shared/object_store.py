from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from src.shared.config import get_settings
from src.shared.cos_network import (
    configure_cos_sdk_logging,
    cos_config_proxies,
    ensure_cos_no_proxy,
)


class ObjectStoreError(Exception):
    pass


@dataclass(slots=True)
class StoredObject:
    backend: str
    key: str
    url: str
    etag: str = ""


class ObjectStore(Protocol):
    def is_enabled(self) -> bool: ...

    def backend_name(self) -> str: ...

    def put_bytes(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str = "",
    ) -> StoredObject: ...

    def get_bytes(self, key: str) -> bytes: ...

    def delete_object(self, key: str) -> None: ...


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


def _object_key(key: str, prefix: str) -> str:
    trimmed = key.strip().lstrip("/")
    if not trimmed:
        return prefix or ""
    if prefix and (trimmed == prefix or trimmed.startswith(f"{prefix}/")):
        return trimmed
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


def _load_cos_sdk():
    configure_cos_sdk_logging()
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ModuleNotFoundError as exc:  # pragma: no cover - defensive wrapper
        raise ObjectStoreError("cos-python-sdk-v5 package is not installed") from exc
    return CosConfig, CosS3Client


class DisabledObjectStore:
    def is_enabled(self) -> bool:
        return False

    def backend_name(self) -> str:
        return "local"

    def put_bytes(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str = "",
    ) -> StoredObject:
        raise ObjectStoreError("object store is not configured")

    def get_bytes(self, key: str) -> bytes:
        raise ObjectStoreError("object store is not configured")

    def delete_object(self, key: str) -> None:
        return None


class CosObjectStore:
    def __init__(self) -> None:
        self._cos = _cos_settings()

    def is_enabled(self) -> bool:
        return bool(
            self._cos["secret_id"]
            and self._cos["secret_key"]
            and self._cos["bucket"]
            and self._cos["region"]
        )

    def backend_name(self) -> str:
        return "cos"

    def _build_client(self):
        CosConfig, CosS3Client = _load_cos_sdk()
        ensure_cos_no_proxy(
            endpoint=self._cos["endpoint"],
            bucket=self._cos["bucket"],
            region=self._cos["region"],
        )
        config = CosConfig(**_cos_config_kwargs(self._cos))
        return CosS3Client(config)

    def put_bytes(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str = "",
    ) -> StoredObject:
        if not self.is_enabled():
            raise ObjectStoreError("COS is not configured")

        object_key = _object_key(key, self._cos["prefix"])
        client = self._build_client()
        try:
            response = client.put_object(
                Bucket=self._cos["bucket"],
                Key=object_key,
                Body=content,
                ContentType=content_type or None,
            )
        except Exception as exc:  # pragma: no cover - SDK defensive wrapper
            raise ObjectStoreError(f"{type(exc).__name__}: {exc}") from exc

        endpoint = _cos_endpoint(
            self._cos["endpoint"],
            self._cos["bucket"],
            self._cos["region"],
        )
        return StoredObject(
            backend="cos",
            key=object_key,
            url=f"{endpoint}/{object_key}",
            etag=str(response.get("ETag", "") or ""),
        )

    def get_bytes(self, key: str) -> bytes:
        if not self.is_enabled():
            raise ObjectStoreError("COS is not configured")

        object_key = _object_key(key, self._cos["prefix"])
        client = self._build_client()
        try:
            response = client.get_object(Bucket=self._cos["bucket"], Key=object_key)
            body = response.get("Body")
            if body is None:
                raise ObjectStoreError("COS response body is empty")
            if hasattr(body, "get_raw_stream"):
                return body.get_raw_stream().read()
            if hasattr(body, "read"):
                return body.read()
            raise ObjectStoreError("unsupported COS response body")
        except ObjectStoreError:
            raise
        except Exception as exc:  # pragma: no cover - SDK defensive wrapper
            raise ObjectStoreError(f"{type(exc).__name__}: {exc}") from exc

    def delete_object(self, key: str) -> None:
        if not self.is_enabled():
            return
        object_key = _object_key(key, self._cos["prefix"])
        client = self._build_client()
        try:
            client.delete_object(Bucket=self._cos["bucket"], Key=object_key)
        except Exception as exc:  # pragma: no cover - SDK defensive wrapper
            raise ObjectStoreError(f"{type(exc).__name__}: {exc}") from exc


def build_object_store() -> ObjectStore:
    store = CosObjectStore()
    if store.is_enabled():
        return store
    return DisabledObjectStore()
