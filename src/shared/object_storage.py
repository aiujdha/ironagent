from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from src.shared.config import WorkspaceObjectStorageBackend, get_settings
from src.shared.cos_network import (
    configure_cos_sdk_logging,
    cos_config_proxies,
    ensure_cos_no_proxy,
)


@dataclass(slots=True)
class StoredObject:
    backend: WorkspaceObjectStorageBackend
    key: str
    url: str


class WorkspaceObjectStorage:
    def __init__(self) -> None:
        settings = get_settings()
        self.backend = settings.workspace_object_storage_backend
        self.local_root = settings.workspace_object_storage_root
        self.prefix = settings.workspace_object_storage_prefix.strip().strip("/")

    def put_bytes(self, *, key: str, content: bytes, content_type: str = "") -> StoredObject:
        object_key = self._full_key(key)
        if self.backend == "cos":
            return self._put_to_cos(
                object_key=object_key,
                content=content,
                content_type=content_type,
            )
        if self.backend == "obs":
            return self._put_to_obs(
                object_key=object_key,
                content=content,
                content_type=content_type,
            )
        return self._put_to_local(object_key=object_key, content=content)

    def get_bytes(self, *, key: str) -> bytes | None:
        object_key = self._full_key(key)
        if self.backend == "cos":
            return self._get_from_cos(object_key)
        if self.backend == "obs":
            return self._get_from_obs(object_key)
        return self._get_from_local(object_key)

    def delete(self, *, key: str) -> None:
        object_key = self._full_key(key)
        if self.backend == "cos":
            self._delete_from_cos(object_key)
            return
        if self.backend == "obs":
            self._delete_from_obs(object_key)
            return
        self._delete_from_local(object_key)

    def _full_key(self, key: str) -> str:
        trimmed = key.strip().lstrip("/")
        if not trimmed:
            raise ValueError("object storage key must not be empty")
        if self.prefix and trimmed == self.prefix:
            return trimmed
        if self.prefix and trimmed.startswith(f"{self.prefix}/"):
            return trimmed
        if self.prefix:
            return f"{self.prefix}/{trimmed}"
        return trimmed

    def _put_to_local(self, *, object_key: str, content: bytes) -> StoredObject:
        path = self.local_root / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return StoredObject(backend="local", key=object_key, url=str(path.resolve()))

    def _get_from_local(self, object_key: str) -> bytes | None:
        path = self.local_root / object_key
        if not path.exists():
            return None
        return path.read_bytes()

    def _delete_from_local(self, object_key: str) -> None:
        path = self.local_root / object_key
        if path.exists():
            path.unlink()

    def _put_to_cos(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> StoredObject:
        client = _build_cos_client()
        if client is None:
            raise ValueError("COS is not configured or cos-python-sdk-v5 is not installed")
        settings = get_settings()
        response = client.put_object(
            Bucket=settings.cos_bucket.strip(),
            Body=content,
            Key=object_key,
            ContentType=content_type or "application/octet-stream",
        )
        if response.get("ETag") is None:
            raise ValueError(f"COS upload failed: {response}")
        endpoint = _normalize_endpoint(
            settings.cos_endpoint.strip(),
            fallback=f"https://{settings.cos_bucket.strip()}.cos.{settings.cos_region.strip()}.myqcloud.com",
        )
        return StoredObject(backend="cos", key=object_key, url=f"{endpoint}/{object_key}")

    def _get_from_cos(self, object_key: str) -> bytes | None:
        client = _build_cos_client()
        if client is None:
            return None
        settings = get_settings()
        stream = client.get_object(Bucket=settings.cos_bucket.strip(), Key=object_key)
        body = stream.get("Body")
        if body is None:
            return None
        return body.get_raw_stream().read()

    def _delete_from_cos(self, object_key: str) -> None:
        client = _build_cos_client()
        if client is None:
            return
        settings = get_settings()
        client.delete_object(Bucket=settings.cos_bucket.strip(), Key=object_key)

    def _put_to_obs(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> StoredObject:
        client = _build_obs_client()
        if client is None:
            raise ValueError("OBS is not configured or obs SDK is not installed")
        settings = get_settings()
        response = client.putContent(
            settings.obs_bucket.strip(),
            object_key,
            content=content,
            headers={"contentType": content_type or "application/octet-stream"},
        )
        if getattr(response, "status", 0) >= 300:
            raise ValueError(f"OBS upload failed: {getattr(response, 'errorMessage', response)}")
        endpoint = _normalize_endpoint(settings.obs_endpoint.strip())
        return StoredObject(backend="obs", key=object_key, url=f"{endpoint}/{object_key}")

    def _get_from_obs(self, object_key: str) -> bytes | None:
        client = _build_obs_client()
        if client is None:
            return None
        settings = get_settings()
        response = client.getObject(
            settings.obs_bucket.strip(),
            object_key,
            loadStreamInMemory=True,
        )
        if getattr(response, "status", 0) >= 300:
            return None
        body = getattr(response, "body", None)
        if body is None:
            return None
        return body.response.read()

    def _delete_from_obs(self, object_key: str) -> None:
        client = _build_obs_client()
        if client is None:
            return
        settings = get_settings()
        client.deleteObject(settings.obs_bucket.strip(), object_key)


def _normalize_endpoint(endpoint: str, *, fallback: str = "") -> str:
    trimmed = endpoint.rstrip("/")
    if trimmed:
        return trimmed
    return fallback.rstrip("/")


def _cos_url_location(url: str) -> tuple[str, str, str, str] | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    parts = parsed.netloc.split(".")
    if len(parts) < 5 or parts[1] != "cos":
        return None
    object_key = parsed.path.lstrip("/")
    if not object_key:
        return None
    return parts[0], parts[2], object_key, parsed.scheme


def download_cos_url_bytes(url: str) -> bytes | None:
    location = _cos_url_location(url)
    if location is None:
        return None
    bucket, region, object_key, scheme = location
    settings = get_settings()
    if not (settings.cos_secret_id.strip() and settings.cos_secret_key.strip()):
        return None
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ModuleNotFoundError:
        return None
    client = CosS3Client(
        CosConfig(
            Region=region,
            SecretId=settings.cos_secret_id.strip(),
            SecretKey=settings.cos_secret_key.strip(),
            Token=settings.cos_session_token.strip() or None,
            Scheme=scheme,
    )
        )
    response = client.get_object(Bucket=bucket, Key=object_key)
    body = response.get("Body")
    if body is None:
        return None
    return body.get_raw_stream().read()


def _build_cos_client() -> Any | None:
    settings = get_settings()
    if not (
        settings.cos_secret_id.strip()
        and settings.cos_secret_key.strip()
        and settings.cos_bucket.strip()
        and settings.cos_region.strip()
    ):
        return None
    try:
        configure_cos_sdk_logging()
        from qcloud_cos import CosConfig, CosS3Client
    except ModuleNotFoundError:
        return None
    parsed = urlparse(settings.cos_endpoint.strip())
    endpoint_host = parsed.netloc or settings.cos_endpoint.strip()
    kwargs: dict[str, object] = {
        "Region": settings.cos_region.strip(),
        "SecretId": settings.cos_secret_id.strip(),
        "SecretKey": settings.cos_secret_key.strip(),
        "Token": settings.cos_session_token.strip() or None,
        "Scheme": parsed.scheme or "https",
        "Proxies": cos_config_proxies(),
    }
    if endpoint_host and not endpoint_host.startswith(f"{settings.cos_bucket.strip()}."):
        kwargs["Endpoint"] = endpoint_host
    ensure_cos_no_proxy(
        endpoint=settings.cos_endpoint,
        bucket=settings.cos_bucket,
        region=settings.cos_region,
    )
    return CosS3Client(CosConfig(**kwargs))


def _build_obs_client() -> Any | None:
    settings = get_settings()
    if not (
        settings.obs_access_key_id.strip()
        and settings.obs_secret_access_key.strip()
        and settings.obs_bucket.strip()
        and settings.obs_endpoint.strip()
    ):
        return None
    try:
        from obs import ObsClient
    except ModuleNotFoundError:
        return None
    parsed = urlparse(settings.obs_endpoint.strip())
    server = parsed.netloc or settings.obs_endpoint.strip()
    is_secure = (parsed.scheme or "https").lower() == "https"
    return ObsClient(
        access_key_id=settings.obs_access_key_id.strip(),
        secret_access_key=settings.obs_secret_access_key.strip(),
        server=server,
        is_secure=is_secure,
    )
