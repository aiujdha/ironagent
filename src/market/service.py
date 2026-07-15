from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx

from src.assistants.service import AssistantService
from src.market.common.cos_client import (
    cos_key_from_url,
    delete_file_from_cos,
    is_cos_enabled,
    upload_file_to_cos,
)
from src.market.common.package import PackageHandler
from src.market.common.storage import StorageManager
from src.market.common.validator import PackageValidator
from src.market.plugin.models import PluginRecord
from src.market.skill.models import SkillRecord
from src.shared.async_utils import run_sync
from src.shared.blob_storage import (
    _build_client,
    _cos_endpoint,
    _cos_settings,
    _object_key,
    download_file_from_cos,
)
from src.shared.config import get_settings
from src.shared.json_store import read_json_object, write_json_object
from src.shared.logging import get_logger
from src.shared.workspace_paths import runtime_workspace_path
from src.workspace_files.item_index_writer import record_workspace_item_installed
from src.workspace_files.sync_service import WorkspaceSyncService

log = get_logger(__name__)

MarketKind = Literal["skills", "plugins"]
CATALOG_SYNC_TTL_SECONDS = 30.0


def _cos_url_location(path: str) -> tuple[str, str, str, str] | None:
    parsed = urlparse(path.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    parts = parsed.netloc.split(".")
    if len(parts) < 5 or parts[1] != "cos":
        return None
    key = parsed.path.lstrip("/")
    if not key:
        return None
    return parts[0], parts[2], key, parsed.scheme


def download_file_from_cos_url(path: str) -> tuple[bool, bytes | str]:
    location = _cos_url_location(path)
    if location is None:
        return False, "not a COS object URL"
    bucket, region, object_key, scheme = location
    cos = _cos_settings()
    if not cos["secret_id"] or not cos["secret_key"]:
        return False, "COS credentials are not configured"
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ModuleNotFoundError:
        return False, "cos-python-sdk-v5 package is not installed"
    try:
        client = CosS3Client(
            CosConfig(
                Region=region,
                SecretId=cos["secret_id"],
                SecretKey=cos["secret_key"],
                Token=cos["session_token"] or None,
                Scheme=scheme,
        )
            )
        response = client.get_object(Bucket=bucket, Key=object_key)
        body = response.get("Body")
        if body is None:
            return False, "Download failed: missing response body"
        return True, body.get_raw_stream().read()
    except Exception as exc:  # pragma: no cover
        return False, f"{type(exc).__name__}: {exc}"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class InstallResult:
    assistant_id: str
    install_path: str
    install_count: int
    sync_status: Literal["synced", "failed"] = "synced"
    sync_error: str = ""
    runtime_reload_status: Literal["applied", "failed", "skipped"] = "skipped"
    runtime_reload_error: str = ""
    action: Literal["install", "upgrade"] = "install"
    previous_version: str = ""


@dataclass(slots=True)
class DownloadTarget:
    source: Literal["local", "remote"]
    path: str
    filename: str
    cleanup: bool = False


class MarketCatalog:
    def __init__(
        self,
        *,
        skills: list[SkillRecord] | None = None,
        plugins: list[PluginRecord] | None = None,
    ) -> None:
        self.skills = skills or []
        self.plugins = plugins or []

    def model_dump(self) -> dict[str, list[dict[str, object]]]:
        return {
            "skills": [record.model_dump(mode="json") for record in self.skills],
            "plugins": [record.model_dump(mode="json") for record in self.plugins],
        }


class MarketService:
    def __init__(
        self,
        *,
        store_path: Path | None = None,
        package_root: Path | None = None,
        upload_root: Path | None = None,
        assistant_service: AssistantService | None = None,
        workspace_item_index_store: Any | None = None,
    ) -> None:
        settings = get_settings()
        self.storage_backend = (
            "json"
            if store_path is not None or package_root is not None or upload_root is not None
            else settings.storage_backend
        )
        self.database_url = settings.database_url
        self.store_path = store_path or settings.market_store_path
        self.package_root = package_root or settings.market_package_root
        self.upload_root = upload_root or settings.market_upload_root
        self.storage = StorageManager(self.package_root)
        self.assistant_service = assistant_service or AssistantService()
        self.workspace_item_index_store = workspace_item_index_store
        self.workspace_sync_service = WorkspaceSyncService(
            assistant_service=self.assistant_service,
        )
        # Keep the catalog cache instance-scoped so separate stores do not
        # bleed into each other during tests or multi-tenant usage.
        self._catalog_cache: MarketCatalog | None = None
        self._catalog_cache_at: float = 0.0
        self.allow_remote_upload = (
            store_path is None and package_root is None and upload_root is None
        )

    @contextmanager
    def _mysql_connection(self):
        import pymysql

        parsed = urlparse(self.database_url)
        connection = pymysql.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/"),
            charset="utf8mb4",
            autocommit=False,
        )
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_mysql_tables(self) -> None:
        with self._mysql_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS market_skills (
                      id varchar(36) NOT NULL,
                      name varchar(255) NOT NULL,
                      slug varchar(255) NOT NULL,
                      version varchar(64) NOT NULL,
                      description text,
                      author varchar(255) DEFAULT '',
                      tags varchar(255) DEFAULT '',
                      package_path text,
                      local_archive_path text,
                      manifest longtext,
                      status varchar(32) NOT NULL,
                      install_count int NOT NULL DEFAULT 0,
                      storage_backend varchar(32) NOT NULL DEFAULT 'local',
                      created_at datetime NOT NULL,
                      reviewed_at datetime NULL,
                      PRIMARY KEY (id),
                      KEY ix_market_skills_slug (slug),
                      UNIQUE KEY uq_market_skills_slug_version (slug, version)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS market_plugins (
                      id varchar(36) NOT NULL,
                      name varchar(255) NOT NULL,
                      slug varchar(255) NOT NULL,
                      version varchar(64) NOT NULL,
                      description text,
                      author varchar(255) DEFAULT '',
                      tags varchar(255) DEFAULT '',
                      package_path text,
                      local_archive_path text,
                      manifest longtext,
                      status varchar(32) NOT NULL,
                      install_count int NOT NULL DEFAULT 0,
                      storage_backend varchar(32) NOT NULL DEFAULT 'local',
                      created_at datetime NOT NULL,
                      reviewed_at datetime NULL,
                      PRIMARY KEY (id),
                      KEY ix_market_plugins_slug (slug),
                      UNIQUE KEY uq_market_plugins_slug_version (slug, version)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                self._ensure_mysql_version_indexes(cur)

    def _ensure_mysql_version_indexes(self, cur) -> None:
        tables = (
            (
                "market_skills",
                "uq_market_skills_slug",
                "ix_market_skills_slug",
                "uq_market_skills_slug_version",
            ),
            (
                "market_plugins",
                "uq_market_plugins_slug",
                "ix_market_plugins_slug",
                "uq_market_plugins_slug_version",
            ),
        )
        for table, old_unique, slug_index, version_unique in tables:
            cur.execute(f"SHOW INDEX FROM {table} WHERE Key_name = %s", (old_unique,))
            if cur.fetchall():
                cur.execute(f"ALTER TABLE {table} DROP INDEX {old_unique}")
            cur.execute(f"SHOW INDEX FROM {table} WHERE Key_name = %s", (slug_index,))
            if not cur.fetchall():
                cur.execute(f"ALTER TABLE {table} ADD KEY {slug_index} (slug)")
            cur.execute(f"SHOW INDEX FROM {table} WHERE Key_name = %s", (version_unique,))
            if not cur.fetchall():
                cur.execute(f"ALTER TABLE {table} ADD UNIQUE KEY {version_unique} (slug, version)")

    def list_skills(self, *, sync_cos: bool = True) -> list[SkillRecord]:
        return list(self._load_catalog(sync_cos=sync_cos).skills)

    def list_plugins(self, *, sync_cos: bool = True) -> list[PluginRecord]:
        return list(self._load_catalog(sync_cos=sync_cos).plugins)

    def get_skill(self, record_id: str) -> SkillRecord | None:
        return next((record for record in self.list_skills() if record.id == record_id), None)

    def get_plugin(self, record_id: str) -> PluginRecord | None:
        return next((record for record in self.list_plugins() if record.id == record_id), None)

    def upload_skill(
        self,
        *,
        filename: str,
        content: bytes,
        slug: str,
        display_name: str,
        owner: str,
        version: str,
        tags: str,
    ) -> SkillRecord:
        return self._upload_package(
            kind="skills",
            filename=filename,
            content=content,
            slug=slug,
            display_name=display_name,
            owner=owner,
            version=version,
            tags=tags,
        )

    def upload_plugin(
        self,
        *,
        filename: str,
        content: bytes,
        slug: str,
        display_name: str,
        owner: str,
        version: str,
        tags: str,
    ) -> PluginRecord:
        return self._upload_package(
            kind="plugins",
            filename=filename,
            content=content,
            slug=slug,
            display_name=display_name,
            owner=owner,
            version=version,
            tags=tags,
        )

    def approve_skill(self, record_id: str) -> SkillRecord:
        return self._update_status(kind="skills", record_id=record_id, status="approved")

    def approve_plugin(self, record_id: str) -> PluginRecord:
        return self._update_status(kind="plugins", record_id=record_id, status="approved")

    def delete_skill(self, record_id: str) -> None:
        self._delete_record(kind="skills", record_id=record_id)

    def delete_plugin(self, record_id: str) -> None:
        self._delete_record(kind="plugins", record_id=record_id)

    def install_skill(self, record_id: str, assistant_id: str) -> InstallResult:
        return run_sync(self.ainstall_skill(record_id=record_id, assistant_id=assistant_id))

    async def ainstall_skill(self, *, record_id: str, assistant_id: str) -> InstallResult:
        return await self._ainstall_record(
            kind="skills",
            record_id=record_id,
            assistant_id=assistant_id,
        )

    def install_plugin(self, record_id: str, assistant_id: str) -> InstallResult:
        return run_sync(self.ainstall_plugin(record_id=record_id, assistant_id=assistant_id))

    async def ainstall_plugin(self, *, record_id: str, assistant_id: str) -> InstallResult:
        return await self._ainstall_record(
            kind="plugins",
            record_id=record_id,
            assistant_id=assistant_id,
        )

    def get_skill_download_target(self, record_id: str) -> DownloadTarget:
        return self._download_target(kind="skills", record_id=record_id)

    def get_plugin_download_target(self, record_id: str) -> DownloadTarget:
        return self._download_target(kind="plugins", record_id=record_id)

    def _upload_package(
        self,
        *,
        kind: MarketKind,
        filename: str,
        content: bytes,
        slug: str,
        display_name: str,
        owner: str,
        version: str,
        tags: str,
    ) -> SkillRecord | PluginRecord:
        safe_slug = self._slugify(slug or display_name or filename)
        if not safe_slug:
            raise ValueError("slug cannot be empty")

        self.upload_root.mkdir(parents=True, exist_ok=True)
        temp_path = self.upload_root / f"inspect-{safe_slug}-{filename}"
        temp_path.write_bytes(content)
        try:
            manifest = PackageHandler.extract_manifest(temp_path, kind=kind)
        except Exception as exc:
            raise ValueError("package archive must be zip/tar/tar.gz/tgz") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        errors = PackageValidator.validate_manifest_content(manifest) if kind == "skills" else []
        if errors and not display_name.strip():
            raise ValueError("; ".join(errors))

        record_name = (
            display_name.strip()
            or str(manifest.get("name", "")).strip()
            or str(manifest.get("id", "")).strip()
            or safe_slug
        )
        record_version = version.strip() or str(manifest.get("version", "")).strip() or "1.0.0"
        record_description = str(manifest.get("description", "")).strip()
        record_author = owner.strip() or str(manifest.get("author", "")).strip()

        record: SkillRecord | PluginRecord
        if kind == "skills":
            record = SkillRecord(
                name=record_name,
                slug=safe_slug,
                version=record_version,
                description=record_description,
                author=record_author,
                tags=tags.strip(),
                manifest=manifest,
            )
        else:
            record = PluginRecord(
                name=record_name,
                slug=safe_slug,
                version=record_version,
                description=record_description,
                author=record_author,
                tags=tags.strip(),
                manifest=manifest,
            )

        self.upload_root.mkdir(parents=True, exist_ok=True)
        upload_path = self.upload_root / f"{record.id}-{filename}"
        upload_path.write_bytes(content)

        local_package_path = self.storage.save_package(
            kind=kind,
            record_id=record.id,
            filename=filename,
            source_path=upload_path,
        )
        upload_path.unlink(missing_ok=True)

        record.local_archive_path = str(local_package_path)
        record.package_path = str(local_package_path)
        record.storage_backend = "local"

        if self.allow_remote_upload and is_cos_enabled():
            success, result = upload_file_to_cos(
                str(local_package_path),
                f"{kind}/{record.id}/{filename}",
            )
            if success:
                record.package_path = result
                record.storage_backend = "cos"

        catalog = self._load_catalog(sync_cos=False)
        collection = self._collection_for(catalog, kind)
        collection[:] = [
            item
            for item in collection
            if not (item.slug == record.slug and item.version == record.version)
        ]
        collection.append(record)
        self._write_catalog(catalog)
        return record

    def _update_status(
        self,
        *,
        kind: MarketKind,
        record_id: str,
        status: Literal["approved", "rejected"],
    ) -> SkillRecord | PluginRecord:
        catalog = self._load_catalog(sync_cos=False)
        collection = self._collection_for(catalog, kind)
        for index, item in enumerate(collection):
            if item.id != record_id:
                continue
            updated = item.model_copy(update={"status": status, "reviewed_at": utc_now()})
            collection[index] = updated
            self._write_catalog(catalog)
            return updated
        raise ValueError(f"{kind[:-1]} not found")

    def _delete_record(self, *, kind: MarketKind, record_id: str) -> None:
        catalog = self._load_catalog(sync_cos=False)
        collection = self._collection_for(catalog, kind)
        for item in collection:
            if item.id != record_id:
                continue
            local_archive_path = self._resolve_local_archive(kind=kind, record=item)
            if local_archive_path is not None:
                self.storage.delete_package(package_path=local_archive_path)
            if item.storage_backend == "cos" and item.package_path:
                key = self._object_key_from_path(item.package_path)
                if key:
                    delete_file_from_cos(key)
            collection[:] = [record for record in collection if record.id != record_id]
            self._write_catalog(catalog)
            self._catalog_cache = catalog
            self._catalog_cache_at = datetime.now(UTC).timestamp()
            return
        raise ValueError(f"{kind[:-1]} not found")

    def _install_record(
        self,
        *,
        kind: MarketKind,
        record_id: str,
        assistant_id: str,
    ) -> InstallResult:
        return run_sync(
            self._ainstall_record(
                kind=kind,
                record_id=record_id,
                assistant_id=assistant_id,
        )
            )

    async def _ainstall_record(
        self,
        *,
        kind: MarketKind,
        record_id: str,
        assistant_id: str,
    ) -> InstallResult:
        record = self._require_record(kind=kind, record_id=record_id)
        existing_item = await self._aget_installed_workspace_item(
            assistant_id=assistant_id,
            kind=kind,
            slug=record.slug,
        )
        previous_version = (
            existing_item.version
            if existing_item is not None and existing_item.version != "-"
            else ""
        )
        action: Literal["install", "upgrade"] = (
            "upgrade"
            if existing_item is not None and previous_version != record.version
            else "install"
        )
        workspace_root = await self.assistant_service.aresolve_assistant_workspace_root(
            assistant_id
        )
        install_parent = workspace_root / kind
        install_root = install_parent / record.slug

        archive_path = self._materialize_archive(kind=kind, record=record)
        temp_extract_dir = Path(tempfile.mkdtemp(prefix=f"ironagent-{kind}-"))
        stage_root: Path | None = None
        try:
            package_root = PackageHandler.extract_package(
                archive_path,
                temp_extract_dir,
                kind=kind,
            )
            structure_errors = PackageValidator.validate_package_structure(
                package_root,
                kind=kind,
            )
            if structure_errors:
                raise ValueError("; ".join(structure_errors))
            install_parent.mkdir(parents=True, exist_ok=True)
            stage_root = Path(tempfile.mkdtemp(prefix=f".{record.slug}-", dir=install_parent))
            PackageHandler.materialize_package_contents(package_root, stage_root)
            if install_root.exists():
                if install_root.is_dir():
                    rmtree(install_root)
                else:
                    install_root.unlink()
            stage_root.rename(install_root)
            stage_root = None
        finally:
            if stage_root is not None and stage_root.exists():
                rmtree(stage_root, ignore_errors=True)
            if record.local_archive_path is None and archive_path.exists():
                archive_path.unlink(missing_ok=True)
            rmtree(temp_extract_dir, ignore_errors=True)

        sync_status: Literal["synced", "failed"] = "synced"
        sync_errors: list[str] = []
        runtime_reload = await self.assistant_service.areload_workspace_capability(
            assistant_id=assistant_id,
            kind=kind,
            slug=record.slug,
        )
        if runtime_reload.status == "failed":
            sync_status = "failed"
            sync_errors.append(f"runtime reload failed: {runtime_reload.error}")

        # Mirror just the newly installed package into object storage and metadata
        # so installs do not re-scan the assistant's entire workspace. At this point
        # the package is already materialized in the assistant workspace, so a sync
        # failure must be reported as a warning instead of turning the install into
        # a false failure in the UI.
        try:
            await self.workspace_sync_service.async_sync_assistant_workspace_scopes(
                assistant_id,
                [(kind, record.slug)],
            )
        except Exception as exc:
            sync_status = "failed"
            sync_errors.append(f"workspace sync failed: {type(exc).__name__}: {exc}")
            log.warning(
                "market_install_workspace_sync_failed",
                assistant_id=assistant_id,
                kind=kind,
                slug=record.slug,
                error=sync_errors[-1],
            )

        if self.workspace_item_index_store is not None:
            files_count = sum(1 for path in install_root.rglob("*") if path.is_file())
            try:
                await record_workspace_item_installed(
                    store=self.workspace_item_index_store,
                    assistant_id=assistant_id,
                    kind=kind,
                    slug=record.slug,
                    name=record.name,
                    version=record.version,
                    install_path=str(install_root),
                    files_count=files_count,
                    sync_status=sync_status,
                    runtime_reload_status=runtime_reload.status,
                    runtime_reload_error=runtime_reload.error,
                    last_error="; ".join(sync_errors),
                )
            except Exception as exc:
                sync_status = "failed"
                sync_errors.append(
                    f"workspace item index failed: {type(exc).__name__}: {exc}"
                )
                log.warning(
                    "market_install_workspace_item_index_failed",
                    assistant_id=assistant_id,
                    kind=kind,
                    slug=record.slug,
                    error=sync_errors[-1],
                )

        try:
            updated = self._increment_install_count(kind=kind, record_id=record.id)
            install_count = updated.install_count
        except Exception:
            install_count = record.install_count + 1
        return InstallResult(
            assistant_id=assistant_id,
            install_path=runtime_workspace_path(kind, record.slug),
            install_count=install_count,
            sync_status=sync_status,
            sync_error="; ".join(sync_errors),
            runtime_reload_status=runtime_reload.status,
            runtime_reload_error=runtime_reload.error,
            action=action,
            previous_version=previous_version,
        )

    async def _aget_installed_workspace_item(
        self,
        *,
        assistant_id: str,
        kind: MarketKind,
        slug: str,
    ):
        for item in await self.assistant_service.alist_workspace_items(assistant_id):
            if item.kind == kind and item.slug == slug:
                return item
        return None

    def _increment_install_count(
        self,
        *,
        kind: MarketKind,
        record_id: str,
    ) -> SkillRecord | PluginRecord:
        catalog = self._load_catalog(sync_cos=False)
        collection = self._collection_for(catalog, kind)
        for index, item in enumerate(collection):
            if item.id != record_id:
                continue
            updated = item.model_copy(update={"install_count": item.install_count + 1})
            collection[index] = updated
            self._write_catalog(catalog)
            return updated
        raise ValueError(f"{kind[:-1]} not found")

    def _materialize_archive(
        self,
        *,
        kind: MarketKind,
        record: SkillRecord | PluginRecord,
    ) -> Path:
        local_archive_path = self._resolve_local_archive(kind=kind, record=record)
        if local_archive_path is not None:
            return local_archive_path
        if not record.package_path:
            raise ValueError("package archive not found")

        package_path = record.package_path.strip()
        parsed_package_path = urlparse(package_path)
        if parsed_package_path.scheme == "":
            raise ValueError(f"package archive not found: {package_path}")

        suffix = Path(package_path).suffix or ".zip"
        temp_path = self.upload_root / f"download-{record.id}{suffix}"
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        cos_key = cos_key_from_url(package_path)
        if cos_key and is_cos_enabled():
            success, payload = download_file_from_cos(cos_key)
            if success and isinstance(payload, bytes):
                temp_path.write_bytes(payload)
                return temp_path

        if record.storage_backend == "cos" or _cos_url_location(package_path) is not None:
            success, payload = download_file_from_cos_url(package_path)
            if success and isinstance(payload, bytes):
                temp_path.write_bytes(payload)
                return temp_path
            if record.storage_backend == "cos":
                raise ValueError(str(payload))

        response = httpx.get(package_path, follow_redirects=True, timeout=60.0)
        response.raise_for_status()
        temp_path.write_bytes(response.content)
        return temp_path

    def _download_target(self, *, kind: MarketKind, record_id: str) -> DownloadTarget:
        record = self._require_record(kind=kind, record_id=record_id)
        package_path = record.package_path.strip() if record.package_path else ""

        local_archive_path = self._resolve_local_archive(kind=kind, record=record)
        if local_archive_path is not None:
            return DownloadTarget(
                source="local",
                path=str(local_archive_path),
                filename=local_archive_path.name,
            )

        archive_path = self._materialize_archive(kind=kind, record=record)
        filename = (
            self._filename_from_remote_url(package_path, record.slug)
            if package_path.startswith(("http://", "https://"))
            else archive_path.name
        )
        return DownloadTarget(
            source="local",
            path=str(archive_path),
            filename=filename,
            cleanup=True,
        )

    def _resolve_local_archive(
        self,
        *,
        kind: MarketKind,
        record: SkillRecord | PluginRecord,
    ) -> Path | None:
        names: list[str] = []
        for raw_path in (record.local_archive_path, record.package_path):
            if not raw_path:
                continue
            parsed = urlparse(raw_path.strip())
            if parsed.scheme:
                continue
            for candidate in self._local_archive_candidates(
                kind=kind,
                record_id=record.id,
                raw_path=raw_path,
            ):
                if candidate.exists():
                    return candidate
                name = candidate.name
                if name and name not in names:
                    names.append(name)

        package_dir = self.package_root / kind / record.id
        if not package_dir.exists():
            return None
        for name in names:
            candidate = package_dir / name
            if candidate.exists():
                return candidate
        archives = [
            path
            for path in package_dir.iterdir()
            if path.is_file()
            and (
                path.name.lower().endswith(".zip")
                or path.name.lower().endswith(".tgz")
                or path.name.lower().endswith(".tar.gz")
                or path.name.lower().endswith(".tar")
            )
        ]
        if len(archives) == 1:
            return archives[0]
        return None

    def _local_archive_candidates(
        self,
        *,
        kind: MarketKind,
        record_id: str,
        raw_path: str,
    ) -> list[Path]:
        raw = raw_path.strip()
        if not raw:
            return []
        original = Path(raw).expanduser()
        candidates: list[Path] = []
        if original.is_absolute():
            candidates.append(original)
        else:
            candidates.append(self.package_root / original)

        relative = self._archive_relative_path(kind=kind, record_id=record_id, raw_path=raw)
        if relative is not None:
            candidates.append(self.package_root / relative)
        elif original.name:
            candidates.append(self.package_root / kind / record_id / original.name)

        deduped: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                deduped.append(candidate)
        return deduped

    def _archive_relative_path(
        self,
        *,
        kind: MarketKind,
        record_id: str,
        raw_path: str,
    ) -> Path | None:
        parts = Path(raw_path).parts
        for index in range(0, max(len(parts) - 1, 0)):
            if parts[index] == kind and parts[index + 1] == record_id:
                return Path(*parts[index:])
        return None

    def _filename_from_remote_url(self, url: str, fallback_slug: str) -> str:
        parsed = urlparse(url)
        return Path(parsed.path).name or f"{fallback_slug}.zip"

    def _require_record(self, *, kind: MarketKind, record_id: str) -> SkillRecord | PluginRecord:
        if kind == "skills":
            record = next(
                (
                    item
                    for item in self._load_catalog(sync_cos=False).skills
                    if item.id == record_id
                ),
                None,
            )
        else:
            record = next(
                (
                    item
                    for item in self._load_catalog(sync_cos=False).plugins
                    if item.id == record_id
                ),
                None,
            )
        if record is None:
            raise ValueError(f"{kind[:-1]} not found")
        return record

    def _load_catalog(self, *, sync_cos: bool = True) -> MarketCatalog:
        now = datetime.now(UTC).timestamp()
        if self._catalog_cache is not None and (
            not sync_cos or (now - self._catalog_cache_at) < CATALOG_SYNC_TTL_SECONDS
        ):
            return self._catalog_cache

        if self.storage_backend == "mysql":
            self._ensure_mysql_tables()
            with self._mysql_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, name, slug, version, description, author, tags,
                               package_path, local_archive_path, manifest, status,
                               install_count, storage_backend, created_at, reviewed_at
                        FROM market_skills
                        ORDER BY created_at ASC, slug ASC
                        """
                    )
                    skill_rows = cur.fetchall()
                    cur.execute(
                        """
                        SELECT id, name, slug, version, description, author, tags,
                               package_path, local_archive_path, manifest, status,
                               install_count, storage_backend, created_at, reviewed_at
                        FROM market_plugins
                        ORDER BY created_at ASC, slug ASC
                        """
                    )
                    plugin_rows = cur.fetchall()

            if not skill_rows and not plugin_rows:
                payload = read_json_object(self.store_path, {"skills": [], "plugins": []})
                raw_skills = payload.get("skills", []) if isinstance(payload, dict) else []
                raw_plugins = payload.get("plugins", []) if isinstance(payload, dict) else []
                catalog = MarketCatalog(
                    skills=[
                        SkillRecord.model_validate(item)
                        for item in raw_skills
                        if isinstance(item, dict)
                    ],
                    plugins=[
                        PluginRecord.model_validate(item)
                        for item in raw_plugins
                        if isinstance(item, dict)
                    ],
                )
                if catalog.skills or catalog.plugins:
                    self._write_catalog(catalog)
            else:
                catalog = MarketCatalog(
                    skills=[
                        SkillRecord(
                            id=row[0],
                            name=row[1],
                            slug=row[2],
                            version=row[3],
                            description=row[4] or "",
                            author=row[5] or "",
                            tags=row[6] or "",
                            package_path=row[7],
                            local_archive_path=row[8],
                            manifest=json.loads(row[9]) if row[9] else {},
                            status=row[10],
                            install_count=row[11] or 0,
                            storage_backend=row[12],
                            created_at=row[13],
                            reviewed_at=row[14],
                        )
                        for row in skill_rows
                    ],
                    plugins=[
                        PluginRecord(
                            id=row[0],
                            name=row[1],
                            slug=row[2],
                            version=row[3],
                            description=row[4] or "",
                            author=row[5] or "",
                            tags=row[6] or "",
                            package_path=row[7],
                            local_archive_path=row[8],
                            manifest=json.loads(row[9]) if row[9] else {},
                            status=row[10],
                            install_count=row[11] or 0,
                            storage_backend=row[12],
                            created_at=row[13],
                            reviewed_at=row[14],
                        )
                        for row in plugin_rows
                    ],
                )
            if sync_cos:
                catalog = self._sync_catalog_from_cos(catalog)
            self._catalog_cache = catalog
            self._catalog_cache_at = now
            return catalog

        payload = read_json_object(self.store_path, {"skills": [], "plugins": []})
        raw_skills = payload.get("skills", []) if isinstance(payload, dict) else []
        raw_plugins = payload.get("plugins", []) if isinstance(payload, dict) else []
        catalog = MarketCatalog(
            skills=[
                SkillRecord.model_validate(item) for item in raw_skills if isinstance(item, dict)
            ],
            plugins=[
                PluginRecord.model_validate(item) for item in raw_plugins if isinstance(item, dict)
            ],
        )
        if sync_cos:
            catalog = self._sync_catalog_from_cos(catalog)
        self._catalog_cache = catalog
        self._catalog_cache_at = now
        return catalog

    def _write_catalog(self, catalog: MarketCatalog) -> None:
        if self.storage_backend == "mysql":
            self._ensure_mysql_tables()
            with self._mysql_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM market_skills")
                    cur.execute("DELETE FROM market_plugins")
                    if catalog.skills:
                        cur.executemany(
                            """
                            INSERT INTO market_skills
                            (id, name, slug, version, description, author, tags, package_path,
                             local_archive_path, manifest, status, install_count, storage_backend,
                             created_at, reviewed_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    item.id,
                                    item.name,
                                    item.slug,
                                    item.version,
                                    item.description,
                                    item.author,
                                    item.tags,
                                    item.package_path,
                                    item.local_archive_path,
                                    json.dumps(item.manifest, ensure_ascii=False),
                                    item.status,
                                    item.install_count,
                                    item.storage_backend,
                                    item.created_at,
                                    item.reviewed_at,
                                )
                                for item in catalog.skills
                            ],
                        )
                    if catalog.plugins:
                        cur.executemany(
                            """
                            INSERT INTO market_plugins
                            (id, name, slug, version, description, author, tags, package_path,
                             local_archive_path, manifest, status, install_count, storage_backend,
                             created_at, reviewed_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    item.id,
                                    item.name,
                                    item.slug,
                                    item.version,
                                    item.description,
                                    item.author,
                                    item.tags,
                                    item.package_path,
                                    item.local_archive_path,
                                    json.dumps(item.manifest, ensure_ascii=False),
                                    item.status,
                                    item.install_count,
                                    item.storage_backend,
                                    item.created_at,
                                    item.reviewed_at,
                                )
                                for item in catalog.plugins
                            ],
                        )
            self._catalog_cache = catalog
            self._catalog_cache_at = datetime.now(UTC).timestamp()
            return
        write_json_object(self.store_path, catalog.model_dump())
        self._catalog_cache = catalog
        self._catalog_cache_at = datetime.now(UTC).timestamp()

    def _collection_for(
        self,
        catalog: MarketCatalog,
        kind: MarketKind,
    ) -> list[SkillRecord] | list[PluginRecord]:
        return catalog.skills if kind == "skills" else catalog.plugins

    def _object_key_from_path(self, path: str) -> str | None:
        return cos_key_from_url(path)

    def _sync_catalog_from_cos(self, catalog: MarketCatalog) -> MarketCatalog:
        if not self.allow_remote_upload or not is_cos_enabled():
            return catalog

        changed = False
        discovered_skills = self._dedupe_records_by_slug_version(
            self._discover_cos_records(kind="skills")
        )
        for item in discovered_skills:
            changed = (
                self._upsert_discovered_record(
                    collection=catalog.skills,
                    discovered=item,
                )
                or changed
            )

        discovered_plugins = self._dedupe_records_by_slug_version(
            self._discover_cos_records(kind="plugins")
        )
        for item in discovered_plugins:
            changed = (
                self._upsert_discovered_record(
                    collection=catalog.plugins,
                    discovered=item,
                )
                or changed
            )

        if changed:
            self._write_catalog(catalog)
        return catalog

    def _upsert_discovered_record(
        self,
        *,
        collection: list[SkillRecord] | list[PluginRecord],
        discovered: SkillRecord | PluginRecord,
    ) -> bool:
        for index, existing in enumerate(collection):
            if existing.id == discovered.id or (
                existing.slug == discovered.slug and existing.version == discovered.version
            ):
                updated = discovered.model_copy(
                    update={
                        "install_count": max(existing.install_count, discovered.install_count),
                        "status": existing.status or discovered.status,
                        "reviewed_at": existing.reviewed_at or discovered.reviewed_at,
                    }
                )
                if existing.model_dump(mode="json") != updated.model_dump(mode="json"):
                    collection[index] = updated
                    return True
                return False

        collection.append(discovered)
        return True

    def _discover_cos_records(self, *, kind: MarketKind) -> list[SkillRecord] | list[PluginRecord]:
        client = _build_client()
        if client is None:
            return []

        cos = _cos_settings()
        prefix = _object_key(kind, cos["prefix"])
        marker = ""
        grouped: dict[str, tuple[str, datetime | None]] = {}
        while True:
            kwargs: dict[str, object] = {
                "Bucket": cos["bucket"],
                "Prefix": f"{prefix}/",
                "MaxKeys": 1000,
            }
            if marker:
                kwargs["Marker"] = marker
            response = client.list_objects(**kwargs)
            contents = response.get("Contents") or []
            for entry in contents:
                key = str(entry.get("Key", "")).strip()
                rel = key[len(prefix) + 1 :] if key.startswith(f"{prefix}/") else ""
                if not rel or "/" not in rel:
                    continue
                record_id, filename = rel.split("/", 1)
                lower = filename.lower()
                if not (
                    lower.endswith(".zip")
                    or lower.endswith(".tgz")
                    or lower.endswith(".tar.gz")
                    or lower.endswith(".tar")
                ):
                    continue
                last_modified = entry.get("LastModified")
                grouped.setdefault(record_id, (rel, last_modified))
            if response.get("IsTruncated") != "true":
                break
            marker = str(response.get("NextMarker", "")).strip()
            if not marker:
                break

        items: list[SkillRecord] | list[PluginRecord] = []
        endpoint = _cos_endpoint(cos["endpoint"], cos["bucket"], cos["region"])
        for record_id, (relative_key, last_modified) in grouped.items():
            object_url = f"{endpoint}/{_object_key(f'{kind}/{relative_key}', cos['prefix'])}"
            record = self._record_from_cos_package(
                kind=kind,
                record_id=record_id,
                relative_key=relative_key,
                object_url=object_url,
                last_modified=last_modified,
            )
            if record is not None:
                items.append(record)
        return items

    def _record_from_cos_package(
        self,
        *,
        kind: MarketKind,
        record_id: str,
        relative_key: str,
        object_url: str,
        last_modified: datetime | str | None,
    ) -> SkillRecord | PluginRecord | None:
        suffix = Path(relative_key).suffix or ".zip"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            tmp_path = Path(handle.name)
        try:
            success, payload = download_file_from_cos(f"{kind}/{relative_key}")
            if not success or not isinstance(payload, bytes):
                raise ValueError(str(payload))
            tmp_path.write_bytes(payload)
            manifest = PackageHandler.extract_manifest(tmp_path, kind=kind)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            return None
        finally:
            tmp_path.unlink(missing_ok=True)

        filename = Path(relative_key).name
        stem = filename
        for suffix in (".tar.gz", ".tgz", ".zip", ".tar"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        name = str(manifest.get("name", "")).strip() or stem or record_id
        version = str(manifest.get("version", "")).strip() or "1.0.0"
        description = str(manifest.get("description", "")).strip()
        author = str(manifest.get("author", "")).strip()
        created_at = last_modified if isinstance(last_modified, datetime) else utc_now()
        reviewed_at = created_at
        slug = self._slugify(name) or self._slugify(stem) or record_id
        payload = dict(
            id=record_id,
            name=name,
            slug=slug,
            version=version,
            description=description,
            author=author,
            tags="cos-sync",
            package_path=object_url,
            local_archive_path=None,
            manifest=manifest,
            status="approved",
            install_count=0,
            storage_backend="cos",
            created_at=created_at,
            reviewed_at=reviewed_at,
        )
        if kind == "skills":
            return SkillRecord(**payload)
        return PluginRecord(**payload)

    def _dedupe_records_by_slug_version(
        self,
        records: list[SkillRecord] | list[PluginRecord],
    ) -> list[SkillRecord] | list[PluginRecord]:
        latest_by_slug_version: dict[tuple[str, str], SkillRecord | PluginRecord] = {}
        for item in records:
            key = (item.slug, item.version)
            current = latest_by_slug_version.get(key)
            if current is None or item.created_at >= current.created_at:
                latest_by_slug_version[key] = item
        return list(latest_by_slug_version.values())

    def _slugify(self, value: str) -> str:
        lowered = value.strip().lower()
        safe = "".join(ch if ch.isalnum() else "-" for ch in lowered)
        return "-".join(part for part in safe.split("-") if part)
