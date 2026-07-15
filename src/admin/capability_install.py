from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.assistants.service import AssistantService
from src.market.service import InstallResult, MarketService
from src.workspace_files.item_index import WorkspaceItemIndexStore

CapabilityKind = Literal["skills", "plugins"]


@dataclass(slots=True)
class AssistantCapabilityInstallResult:
    assistant_id: str
    kind: CapabilityKind
    capability_id: str
    slug: str
    name: str
    version: str
    install_path: str
    install_count: int
    sync_status: str
    sync_error: str
    runtime_reload_status: str
    runtime_reload_error: str
    action: str
    previous_version: str


class AssistantCapabilityInstallService:
    def __init__(
        self,
        *,
        assistant_service: AssistantService | None = None,
        market_service: MarketService | None = None,
        workspace_item_index_store: Any | None = None,
    ) -> None:
        self.assistant_service = assistant_service or AssistantService()
        self.workspace_item_index_store = workspace_item_index_store or WorkspaceItemIndexStore()
        self.market_service = market_service or MarketService(
            assistant_service=self.assistant_service,
            workspace_item_index_store=self.workspace_item_index_store,
        )

    async def ainstall_market_record(
        self,
        *,
        assistant_id: str,
        kind: str,
        record_id: str,
    ) -> AssistantCapabilityInstallResult:
        normalized_kind = normalize_capability_kind(kind)
        record_id = record_id.strip()
        if not record_id:
            raise ValueError("record_id is required")
        record = self._record_for(normalized_kind, record_id)
        if record is None:
            raise ValueError(f"{normalized_kind[:-1]} not found")
        result = await self._install_record(
            assistant_id=assistant_id,
            kind=normalized_kind,
            record_id=record.id,
        )
        return _install_result_payload(
            assistant_id=assistant_id,
            kind=normalized_kind,
            record=record,
            result=result,
        )

    async def ainstall_uploaded_package(
        self,
        *,
        assistant_id: str,
        kind: str,
        filename: str,
        content: bytes,
        slug: str = "",
        display_name: str = "",
        owner: str = "",
        version: str = "",
    ) -> AssistantCapabilityInstallResult:
        normalized_kind = normalize_capability_kind(kind)
        safe_filename = safe_package_filename(filename)
        safe_slug = (
            slugify_capability_slug(slug)
            or archive_slug(safe_filename)
            or f"capability-{hashlib.sha256(content).hexdigest()[:12]}"
        )
        if not safe_slug:
            raise ValueError("slug cannot be empty")
        record_name = display_name.strip()
        record_version = version.strip()
        record: Any
        if normalized_kind == "skills":
            record = self.market_service.upload_skill(
                filename=safe_filename,
                content=content,
                slug=safe_slug,
                display_name=record_name,
                owner=owner,
                version=record_version,
                tags="admin-web,direct-install",
            )
            record = self.market_service.approve_skill(record.id)
        else:
            record = self.market_service.upload_plugin(
                filename=safe_filename,
                content=content,
                slug=safe_slug,
                display_name=record_name,
                owner=owner,
                version=record_version,
                tags="admin-web,direct-install",
            )
            record = self.market_service.approve_plugin(record.id)
        result = await self._install_record(
            assistant_id=assistant_id,
            kind=normalized_kind,
            record_id=record.id,
        )
        return _install_result_payload(
            assistant_id=assistant_id,
            kind=normalized_kind,
            record=record,
            result=result,
        )

    def _record_for(self, kind: CapabilityKind, record_id: str) -> Any | None:
        if kind == "skills":
            return self.market_service.get_skill(record_id)
        return self.market_service.get_plugin(record_id)

    async def _install_record(
        self,
        *,
        assistant_id: str,
        kind: CapabilityKind,
        record_id: str,
    ) -> InstallResult:
        if kind == "skills":
            return await self.market_service.ainstall_skill(
                record_id=record_id,
                assistant_id=assistant_id,
            )
        return await self.market_service.ainstall_plugin(
            record_id=record_id,
            assistant_id=assistant_id,
        )


def normalize_capability_kind(kind: str) -> CapabilityKind:
    normalized = kind.strip().lower()
    if normalized == "skill":
        return "skills"
    if normalized == "plugin":
        return "plugins"
    if normalized in {"skills", "plugins"}:
        return normalized  # type: ignore[return-value]
    raise ValueError("invalid kind")


def safe_package_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    return name or "capability-package.zip"


def slugify_capability_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()


def archive_slug(filename: str) -> str:
    lower_name = filename.lower()
    stem = filename
    for suffix in (".tar.gz", ".tgz", ".zip", ".tar"):
        if lower_name.endswith(suffix):
            stem = filename[: -len(suffix)]
            break
    return slugify_capability_slug(stem)


def _install_result_payload(
    *,
    assistant_id: str,
    kind: CapabilityKind,
    record: Any,
    result: InstallResult,
) -> AssistantCapabilityInstallResult:
    return AssistantCapabilityInstallResult(
        assistant_id=assistant_id,
        kind=kind,
        capability_id=str(getattr(record, "id", "") or ""),
        slug=str(getattr(record, "slug", "") or ""),
        name=str(getattr(record, "name", "") or getattr(record, "slug", "") or ""),
        version=str(getattr(record, "version", "") or ""),
        install_path=result.install_path,
        install_count=result.install_count,
        sync_status=result.sync_status,
        sync_error=result.sync_error,
        runtime_reload_status=result.runtime_reload_status,
        runtime_reload_error=result.runtime_reload_error,
        action=result.action,
        previous_version=result.previous_version,
    )
