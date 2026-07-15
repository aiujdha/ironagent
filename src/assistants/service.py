import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copytree, move, rmtree
from typing import Any, Literal

from src.assistants.models import CAPABILITY_PROFILES, AssistantRecord, CapabilityProfileName
from src.runtime.factory import build_runtime_provisioner
from src.runtime.models import (
    AssistantIronAgentConfig,
    AssistantRuntimeRecord,
    AssistantWorkspaceConfig,
    RuntimeStatusName,
    desired_state_for_status,
    sync_state_for_runtime,
)
from src.runtime.provisioner import (
    RUNTIME_API_KEY_PLACEHOLDER,
    AssistantRuntimeProvisioner,
    RuntimeProbeResult,
    is_placeholder_api_key,
)
from src.shared.async_utils import run_sync
from src.shared.config import get_settings
from src.shared.repositories import (
    AssistantRepository,
    JsonAssistantRepository,
    build_assistant_repository,
)
from src.shared.workspace_roots import get_workspace_root, normalize_workspace_slug


def utc_now() -> datetime:
    return datetime.now(UTC)


# Placeholder the admin UI echoes back in place of a configured-but-hidden API
# key. Treated as "not supplied" so it never overwrites the stored secret.
MASKED_API_KEY = "******"


DEFAULT_ASSISTANTS: tuple[dict[str, str], ...] = (
    {
        "assistant_id": "demo-main",
        "name": "Demo Main Assistant",
        "capability_profile": "onlyread",
        "notes": "Default assistant for Phase 1 demos.",
    },
)
RESTART_RECOVERABLE_ERRORS = {
    "container health is unhealthy",
    "gateway endpoint not published yet",
}

WorkspaceItemKind = Literal["skills", "plugins"]
DISABLED_WORKSPACE_ROOT = ".ironagent-disabled"
DISABLED_WORKSPACE_ROOTS = (DISABLED_WORKSPACE_ROOT,)


@dataclass(slots=True)
class AssistantDeleteResult:
    assistant_id: str
    container_name: str
    container_removed: bool
    workspace_removed: bool
    state_removed: bool
    soft_deleted: bool
    runtime_disabled: bool
    deleted_at: datetime


@dataclass(slots=True)
class AssistantWorkspaceItem:
    assistant_id: str
    kind: WorkspaceItemKind
    slug: str
    name: str
    version: str
    enabled: bool
    install_path: str


@dataclass(slots=True)
class AssistantWorkspaceReuseResult:
    source_assistant_id: str
    target_assistant_id: str
    kind: WorkspaceItemKind
    slug: str
    install_path: str


@dataclass(slots=True)
class AssistantWorkspaceRemoveResult:
    assistant_id: str
    kind: WorkspaceItemKind
    slug: str
    removed: bool


@dataclass(slots=True)
class AssistantRuntimeReloadResult:
    assistant_id: str
    kind: WorkspaceItemKind
    slug: str
    status: Literal["applied", "failed", "skipped"]
    error: str = ""


class AssistantService:
    def __init__(
        self,
        store_path: Path | None = None,
        provisioner: AssistantRuntimeProvisioner | None = None,
        repository: AssistantRepository | None = None,
    ) -> None:
        explicit_store_path = store_path
        settings = get_settings()
        self.store_path = explicit_store_path or settings.assistants_store_path
        if repository is not None:
            self.repository = repository
        elif explicit_store_path is not None:
            self.repository = JsonAssistantRepository(self.store_path)
        else:
            self.repository = build_assistant_repository()
        self.provisioner = provisioner or build_runtime_provisioner()

    def _load_records(self) -> list[AssistantRecord]:
        return run_sync(self._aload_records())

    @staticmethod
    def _active_records(records: list[AssistantRecord]) -> list[AssistantRecord]:
        return [record for record in records if not record.deleted]

    async def _aseed_default_records_for_empty_non_json_repository(
        self,
        records: list[AssistantRecord] | None = None,
    ) -> list[AssistantRecord]:
        if isinstance(self.repository, JsonAssistantRepository):
            return records or []
        existing = records if records is not None else await self.repository.alist_assistants()
        if existing:
            return existing
        if not get_settings().seed_default_assistants:
            return existing
        seeded = [AssistantRecord.model_validate(item) for item in DEFAULT_ASSISTANTS]
        for record in seeded:
            await self.repository.aupsert_assistant(record)
        return seeded

    async def _aload_records(self) -> list[AssistantRecord]:
        if not self.repository.is_initialized():
            seeded = [AssistantRecord.model_validate(item) for item in DEFAULT_ASSISTANTS]
            for record in seeded:
                await self.repository.aupsert_assistant(record)
            return await self._arecover_missing_runtime_records(seeded)
        records = await self.repository.alist_assistants()
        seeded = await self._aseed_default_records_for_empty_non_json_repository(records)
        return await self._arecover_missing_runtime_records(seeded)

    async def _arecover_missing_runtime_records(
        self,
        records: list[AssistantRecord],
    ) -> list[AssistantRecord]:
        if not isinstance(self.repository, JsonAssistantRepository):
            return sorted(records, key=lambda item: item.name.lower())

        known_ids = {record.assistant_id for record in records}
        recovered_records = list(records)

        for config_path in self._runtime_state_config_paths():
            assistant_id = config_path.parent.parent.name.strip()
            if not assistant_id or assistant_id in known_ids:
                continue
            recovered = self._recover_record_from_runtime_state(assistant_id, config_path)
            if recovered is None:
                continue
            await self.repository.aupsert_assistant(recovered)
            recovered_records.append(recovered)
            known_ids.add(assistant_id)

        return sorted(recovered_records, key=lambda item: item.name.lower())

    def _runtime_state_config_paths(self) -> list[Path]:
        runtime_root = get_settings().assistants_runtime_root
        if not runtime_root.exists():
            return []
        return sorted(runtime_root.glob("*/state/ironagent.json"))

    def _recover_record_from_runtime_state(
        self,
        assistant_id: str,
        config_path: Path,
    ) -> AssistantRecord | None:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        name = self._assistant_name_from_runtime_state(payload, assistant_id)
        capability_profile = self._capability_profile_from_runtime_state(payload)
        model_config = self._model_config_from_runtime_state(payload)
        workspace_config = AssistantWorkspaceConfig(
            root_id=get_settings().default_workspace_root_id.strip() or "default",
            workspace_slug=assistant_id,
        )
        probe = self.provisioner.probe_runtime(assistant_id=assistant_id)
        if not probe.container_exists:
            return None
        settings = get_settings()
        workspace_root = self._configured_workspace_root_path(
            AssistantRecord(
                assistant_id=assistant_id,
                name=name,
                capability_profile=capability_profile,
                iron_agent_config=model_config,
                workspace_config=workspace_config,
        )
            )
        observed_at = utc_now()
        runtime = AssistantRuntimeRecord(
            container_name=probe.container_name,
            container_id=probe.container_id,
            workspace_root=str(workspace_root.resolve()),
            workspace_root_id=workspace_config.root_id,
            workspace_slug=assistant_id,
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(config_path.parent.resolve()),
            status=probe.status,
            image=settings.iron_agent_container_image,
            gateway_host=probe.gateway_host,
            gateway_port=probe.gateway_port,
            gateway_url=probe.gateway_url,
            gateway_token=self._gateway_token_from_runtime_state(payload, assistant_id),
            last_error=probe.last_error,
            desired_state=desired_state_for_status(probe.status),
            observed_state=probe.status,
            sync_state=sync_state_for_runtime(
                desired_state=desired_state_for_status(probe.status),
                observed_state=probe.status,
                last_runtime_error=probe.last_error,
            ),
            last_observed_at=observed_at,
            last_reconciled_at=observed_at,
            last_runtime_error=probe.last_error,
            updated_at=observed_at,
        )
        return AssistantRecord(
            assistant_id=assistant_id,
            name=name,
            capability_profile=capability_profile,
            notes="Recovered from local runtime state.",
            iron_agent_config=model_config,
            workspace_config=workspace_config,
            runtime=runtime,
        )

    def _assistant_name_from_runtime_state(
        self,
        payload: dict[str, Any],
        assistant_id: str,
    ) -> str:
        agents = payload.get("agents")
        if isinstance(agents, dict):
            agent_list = agents.get("list")
            if isinstance(agent_list, list):
                for item in agent_list:
                    if not isinstance(item, dict):
                        continue
                    candidate_id = str(item.get("id", "")).strip()
                    candidate_name = str(item.get("name", "")).strip()
                    if candidate_id == assistant_id and candidate_name:
                        return candidate_name
        return assistant_id

    def _capability_profile_from_runtime_state(
        self,
        payload: dict[str, Any],
    ) -> CapabilityProfileName:
        tools = payload.get("tools")
        if not isinstance(tools, dict):
            return "onlyread"
        explicit_profile = self._known_capability_profile(
            tools.get("capability_profile") or tools.get("capabilityProfile")
        )
        if explicit_profile:
            return explicit_profile
        allowed = tools.get("allow")
        allowed_tools = (
            {str(item).strip() for item in allowed if str(item).strip()}
            if isinstance(allowed, list)
            else set()
        )
        if {"exec", "process", "cron", "sessions_spawn", "subagents"} & allowed_tools:
            return "dangerous"
        if {"write", "edit", "apply_patch"} & allowed_tools:
            return "onlywrite"
        return "onlyread"

    def _known_capability_profile(self, raw_profile: Any) -> CapabilityProfileName | None:
        profile = str(raw_profile or "").strip()
        if not profile:
            return None
        if profile in CAPABILITY_PROFILES:
            return profile
        capability_path = get_settings().config_dir / "capabilities" / f"{profile}.yaml"
        return profile if capability_path.exists() else None

    def _model_config_from_runtime_state(
        self,
        payload: dict[str, Any],
    ) -> AssistantIronAgentConfig:
        primary_model = "openai-codex/gpt-5.4"
        provider_id = ""
        provider_base_url = ""
        provider_api_key = ""

        agents = payload.get("agents")
        if isinstance(agents, dict):
            defaults = agents.get("defaults")
            if isinstance(defaults, dict):
                model = defaults.get("model")
                if isinstance(model, dict):
                    candidate = str(model.get("primary", "")).strip()
                    if candidate:
                        primary_model = candidate
                        provider_id = candidate.split("/", 1)[0] if "/" in candidate else ""

        models = payload.get("models")
        if isinstance(models, dict):
            providers = models.get("providers")
            if isinstance(providers, dict) and provider_id:
                provider = providers.get(provider_id)
                if isinstance(provider, dict):
                    provider_base_url = str(provider.get("baseUrl", "")).strip()
                    # The real key is written into headers.Authorization for
                    # openai-completions providers; `apiKey` is only the "***"
                    # placeholder. Recovering the placeholder as a credential
                    # poisons the stored config, so prefer the header value and
                    # drop the placeholder when there is no real key.
                    raw_api_key = str(provider.get("apiKey", "")).strip()
                    header_auth = ""
                    headers = provider.get("headers")
                    if isinstance(headers, dict):
                        header_auth = str(headers.get("Authorization", "")).strip()
                    if raw_api_key == RUNTIME_API_KEY_PLACEHOLDER:
                        provider_api_key = header_auth
                    else:
                        provider_api_key = raw_api_key or header_auth

        return AssistantIronAgentConfig(
            primary_model=primary_model,
            provider_id=provider_id,
            provider_base_url=provider_base_url,
            provider_api_key=provider_api_key,
        )

    def _gateway_token_from_runtime_state(
        self,
        payload: dict[str, Any],
        assistant_id: str,
    ) -> str:
        gateway = payload.get("gateway")
        if isinstance(gateway, dict):
            auth = gateway.get("auth")
            if isinstance(auth, dict):
                token = str(auth.get("token", "")).strip()
                if token:
                    return token
        return self._gateway_token_for_assistant(assistant_id)

    def list_assistants(self) -> list[AssistantRecord]:
        return run_sync(self.alist_assistants())

    async def alist_assistants(self) -> list[AssistantRecord]:
        return await self._aload_records()

    def probe_runtime_states(
        self,
        assistants: list[AssistantRecord] | None = None,
    ) -> dict[str, RuntimeProbeResult]:
        records = assistants if assistants is not None else self._load_records()
        active_records = self._active_records(records)
        return {
            record.assistant_id: self.provisioner.probe_runtime(
                assistant_id=record.assistant_id,
            )
            for record in active_records
        }

    async def aprobe_runtime_states(
        self,
        assistants: list[AssistantRecord] | None = None,
    ) -> dict[str, RuntimeProbeResult]:
        records = assistants if assistants is not None else await self._aload_records()
        active_records = self._active_records(records)
        return {
            record.assistant_id: self.provisioner.probe_runtime(
                assistant_id=record.assistant_id,
            )
            for record in active_records
        }

    def sync_runtime_views(
        self,
        assistants: list[AssistantRecord] | None = None,
    ) -> tuple[list[AssistantRecord], dict[str, RuntimeProbeResult], int]:
        return run_sync(self.async_runtime_views(assistants))

    async def async_runtime_views(
        self,
        assistants: list[AssistantRecord] | None = None,
    ) -> tuple[list[AssistantRecord], dict[str, RuntimeProbeResult], int]:
        records = assistants if assistants is not None else await self._aload_records()
        active_records = self._active_records(records)
        probes = await self.aprobe_runtime_states(active_records) if active_records else {}
        updated_records: list[AssistantRecord] = []
        updated_count = 0

        for record in records:
            if record.deleted:
                updated_records.append(record)
                continue
            probe = probes.get(record.assistant_id)
            if probe is None:
                updated_records.append(record)
                continue
            if self._runtime_matches_probe(record, probe):
                updated_records.append(record)
                continue
            next_record = self._record_with_probe(record, probe)
            await self.repository.aupsert_assistant(next_record)
            updated_records.append(next_record)
            updated_count += 1

        return updated_records, probes, updated_count
    def repair_runtime_state(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.arepair_runtime_state(assistant_id))

    async def arepair_runtime_state(self, assistant_id: str) -> AssistantRecord | None:
        existing = await self.aget_assistant(assistant_id)
        if existing is None or existing.deleted:
            return None
        return await self.aupsert_assistant(
            assistant_id=existing.assistant_id,
            name=existing.name,
            capability_profile=existing.capability_profile,
            notes=existing.notes,
            model_config=existing.iron_agent_config,
            workspace_config=existing.workspace_config,
        )

    def restart_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.arestart_assistant(assistant_id))

    def mark_assistant_restarting(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.amark_assistant_restarting(assistant_id))

    async def amark_assistant_restarting(
        self,
        assistant_id: str,
    ) -> AssistantRecord | None:
        existing = await self.aget_assistant(assistant_id)
        if existing is None or existing.deleted:
            return None

        now = utc_now()
        runtime = existing.runtime.model_copy(
            update={
                "status": "restarting",
                "desired_state": "running",
                "observed_state": "restarting",
                "sync_state": sync_state_for_runtime(
                    desired_state="running",
                    observed_state="restarting",
                ),
                "last_error": "",
                "last_runtime_error": "",
                "last_observed_at": now,
                "last_reconciled_at": now,
                "updated_at": now,
            }
        )
        updated = existing.model_copy(update={"runtime": runtime, "updated_at": now})
        await self.repository.aupsert_assistant(updated)
        return updated

    async def arestart_assistant(self, assistant_id: str) -> AssistantRecord | None:
        existing = await self.aget_assistant(assistant_id)
        if existing is None or existing.deleted:
            return None

        probe = self.provisioner.restart_runtime(assistant_id=existing.assistant_id)
        if self._restart_should_reprovision(probe):
            return await self.aupsert_assistant(
                assistant_id=existing.assistant_id,
                name=existing.name,
                capability_profile=existing.capability_profile,
                notes=existing.notes,
                model_config=existing.iron_agent_config,
                workspace_config=existing.workspace_config,
            )
        if probe.status != "failed":
            # Restart returns a transient probe, which often lands during Docker's
            # health startup window. Immediately re-sync the runtime record so the
            # UI reflects the stable post-restart container metadata.
            return await self.aupsert_assistant(
                assistant_id=existing.assistant_id,
                name=existing.name,
                capability_profile=existing.capability_profile,
                notes=existing.notes,
                model_config=existing.iron_agent_config,
                workspace_config=existing.workspace_config,
            )
        if (
            probe.container_exists
            and probe.last_error in RESTART_RECOVERABLE_ERRORS
        ):
            probe = await self._await_restart_recovery(existing.assistant_id, probe)
            if probe.status != "failed":
                updated = self._record_with_probe(existing, probe, desired_state="running")
                await self.repository.aupsert_assistant(updated)
                return updated

        now = utc_now()
        runtime = existing.runtime.model_copy(
            update={
                "container_name": probe.container_name,
                "container_id": probe.container_id,
                "status": probe.status,
                "gateway_host": probe.gateway_host,
                "gateway_port": probe.gateway_port,
                "gateway_url": probe.gateway_url,
                "last_error": probe.last_error,
                "desired_state": "running",
                "observed_state": probe.status,
                "sync_state": sync_state_for_runtime(
                    desired_state="running",
                    observed_state=probe.status,
                    last_runtime_error=probe.last_error,
                ),
                "last_observed_at": now,
                "last_reconciled_at": now,
                "last_runtime_error": probe.last_error,
                "updated_at": now,
            }
        )
        updated = existing.model_copy(update={"runtime": runtime, "updated_at": now})
        await self.repository.aupsert_assistant(updated)
        if probe.status == "failed" and probe.container_exists:
            refreshed, _, _ = await self.async_runtime_views([updated])
            if refreshed:
                return refreshed[0]
        return updated

    @staticmethod
    def _restart_should_reprovision(probe: RuntimeProbeResult) -> bool:
        if probe.status != "failed" or probe.container_exists:
            return False
        error = probe.last_error.strip()
        return error in {
            "runtime manifest not found",
            "runtime pod deleted; reprovision required",
        } or ("pods " in error and " not found" in error)

    async def _await_restart_recovery(
        self,
        assistant_id: str,
        initial_probe: RuntimeProbeResult,
        *,
        attempts: int = 20,
        interval_seconds: float = 0.5,
    ) -> RuntimeProbeResult:
        latest_probe = initial_probe
        for _ in range(attempts):
            await asyncio.sleep(interval_seconds)
            latest_probe = self.provisioner.probe_runtime(assistant_id=assistant_id)
            if latest_probe.status != "failed":
                return latest_probe
            if latest_probe.last_error not in RESTART_RECOVERABLE_ERRORS:
                return latest_probe

    def start_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.astart_assistant(assistant_id))

    async def astart_assistant(self, assistant_id: str) -> AssistantRecord | None:
        existing = await self.aget_assistant(assistant_id)
        if existing is None or existing.deleted:
            return None
        probe = self.provisioner.start_runtime(assistant_id=existing.assistant_id)
        if not probe.container_exists:
            return await self.aupsert_assistant(
                assistant_id=existing.assistant_id,
                name=existing.name,
                capability_profile=existing.capability_profile,
                notes=existing.notes,
                model_config=existing.iron_agent_config,
                workspace_config=existing.workspace_config,
            )
        if probe.status != "failed":
            probe = self.provisioner._wait_for_runtime_probe(
                assistant_id=existing.assistant_id,
                transient_statuses={"starting", "paused"},
                tolerate_unhealthy=True,
            )
            updated = self._record_with_probe(existing, probe, desired_state="running")
            await self.repository.aupsert_assistant(updated)
            return updated
        updated = self._record_with_probe(existing, probe, desired_state="running")
        await self.repository.aupsert_assistant(updated)
        return updated

    def stop_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.astop_assistant(assistant_id))

    async def astop_assistant(self, assistant_id: str) -> AssistantRecord | None:
        existing = await self.aget_assistant(assistant_id)
        if existing is None or existing.deleted:
            return None
        probe = self.provisioner.stop_runtime(assistant_id=existing.assistant_id)
        updated = self._record_with_probe(existing, probe, desired_state="stopped")
        await self.repository.aupsert_assistant(updated)
        return updated

    def pause_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.apause_assistant(assistant_id))

    async def apause_assistant(self, assistant_id: str) -> AssistantRecord | None:
        existing = await self.aget_assistant(assistant_id)
        if existing is None or existing.deleted:
            return None
        probe = self.provisioner.pause_runtime(assistant_id=existing.assistant_id)
        updated = self._record_with_probe(existing, probe, desired_state="paused")
        await self.repository.aupsert_assistant(updated)
        return updated
    def repair_out_of_sync_runtimes(self) -> tuple[int, int]:
        return run_sync(self.arepair_out_of_sync_runtimes())

    async def arepair_out_of_sync_runtimes(self) -> tuple[int, int]:
        return await self.areconcile_runtime_states()

    def reconcile_runtime_states(self) -> tuple[int, int]:
        return run_sync(self.areconcile_runtime_states())

    async def areconcile_runtime_states(self) -> tuple[int, int]:
        records = await self._aload_records()
        active_records = self._active_records(records)
        probes = await self.aprobe_runtime_states(active_records) if active_records else {}
        repaired = 0
        skipped = 0
        for record in records:
            if record.deleted:
                skipped += 1
                continue
            probe = probes.get(record.assistant_id)
            if probe is None:
                skipped += 1
                continue
            if not self._runtime_needs_repair(record, probe):
                skipped += 1
                continue
            await self.aupsert_assistant(
                assistant_id=record.assistant_id,
                name=record.name,
                capability_profile=record.capability_profile,
                notes=record.notes,
                model_config=record.iron_agent_config,
                workspace_config=record.workspace_config,
            )
            repaired += 1
        return repaired, skipped

    def get_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.aget_assistant(assistant_id))

    async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
        if not self.repository.is_initialized():
            await self._aload_records()
        else:
            await self._aseed_default_records_for_empty_non_json_repository()
        return await self.repository.aget_assistant(assistant_id)

    def resolve_assistant_workspace_root(self, assistant_id: str) -> Path:
        return run_sync(self.aresolve_assistant_workspace_root(assistant_id))

    async def aresolve_assistant_workspace_root(self, assistant_id: str) -> Path:
        assistant = await self.aget_assistant(assistant_id)
        if assistant is None:
            raise ValueError("assistant not found")
        return self._assistant_workspace_root(assistant)

    def list_workspace_items(self, assistant_id: str) -> list[AssistantWorkspaceItem]:
        return run_sync(self.alist_workspace_items(assistant_id))

    async def alist_workspace_items(self, assistant_id: str) -> list[AssistantWorkspaceItem]:
        assistant = await self.aget_assistant(assistant_id)
        if assistant is None:
            raise ValueError("assistant not found")
        workspace_root = self._assistant_workspace_root(assistant)
        items: list[AssistantWorkspaceItem] = []
        for kind in ("skills", "plugins"):
            enabled_root = workspace_root / kind
            items.extend(
                self._collect_workspace_items(
                    assistant_id=assistant.assistant_id,
                    kind=kind,
                    root=enabled_root,
                    enabled=True,
                )
            )
            for disabled_root_name in DISABLED_WORKSPACE_ROOTS:
                items.extend(
                    self._collect_workspace_items(
                        assistant_id=assistant.assistant_id,
                        kind=kind,
                        root=workspace_root / disabled_root_name / kind,
                        enabled=False,
                    )
                )
        return sorted(items, key=lambda item: (item.kind, item.slug.lower(), not item.enabled))

    def set_workspace_item_enabled(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
        enabled: bool,
    ) -> AssistantWorkspaceItem:
        return run_sync(
            self.aset_workspace_item_enabled(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                enabled=enabled,
        )
            )

    async def aset_workspace_item_enabled(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
        enabled: bool,
    ) -> AssistantWorkspaceItem:
        assistant = await self.aget_assistant(assistant_id)
        if assistant is None:
            raise ValueError("assistant not found")
        workspace_root = self._assistant_workspace_root(assistant)
        current = self._find_workspace_item(
            assistant_id=assistant.assistant_id,
            workspace_root=workspace_root,
            kind=kind,
            slug=slug,
        )
        if current is None:
            raise ValueError(f"{kind[:-1]} not found in assistant workspace")
        if current.enabled == enabled:
            return current

        target_root = self._workspace_kind_root(
            workspace_root=workspace_root,
            kind=kind,
            enabled=enabled,
        )
        target_root.mkdir(parents=True, exist_ok=True)
        target_path = target_root / current.slug
        self._remove_workspace_item_variants(
            workspace_root=workspace_root,
            kind=kind,
            slug=current.slug,
            keep_path=Path(current.install_path).resolve(),
        )
        move(current.install_path, target_path)
        return self._build_workspace_item(
            assistant_id=assistant.assistant_id,
            kind=kind,
            path=target_path,
            enabled=enabled,
        )

    def reuse_workspace_item(
        self,
        *,
        source_assistant_id: str,
        target_assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantWorkspaceReuseResult:
        return run_sync(
            self.areuse_workspace_item(
                source_assistant_id=source_assistant_id,
                target_assistant_id=target_assistant_id,
                kind=kind,
                slug=slug,
        )
            )

    async def areuse_workspace_item(
        self,
        *,
        source_assistant_id: str,
        target_assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantWorkspaceReuseResult:
        source = await self.aget_assistant(source_assistant_id)
        if source is None:
            raise ValueError("source assistant not found")
        target = await self.aget_assistant(target_assistant_id)
        if target is None:
            raise ValueError("target assistant not found")
        if source.assistant_id == target.assistant_id:
            raise ValueError("source and target assistant must be different")

        source_workspace_root = self._assistant_workspace_root(source)
        target_workspace_root = self._assistant_workspace_root(target)
        source_item = self._find_workspace_item(
            assistant_id=source.assistant_id,
            workspace_root=source_workspace_root,
            kind=kind,
            slug=slug,
        )
        if source_item is None:
            raise ValueError(f"{kind[:-1]} not found in source assistant workspace")

        target_root = self._workspace_kind_root(
            workspace_root=target_workspace_root,
            kind=kind,
            enabled=True,
        )
        target_root.mkdir(parents=True, exist_ok=True)
        target_path = target_root / source_item.slug
        self._remove_workspace_item_variants(
            workspace_root=target_workspace_root,
            kind=kind,
            slug=source_item.slug,
        )
        copytree(source_item.install_path, target_path)
        return AssistantWorkspaceReuseResult(
            source_assistant_id=source.assistant_id,
            target_assistant_id=target.assistant_id,
            kind=kind,
            slug=source_item.slug,
            install_path=str(target_path),
        )

    def remove_workspace_item(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantWorkspaceRemoveResult:
        return run_sync(
            self.aremove_workspace_item(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
        )
            )

    async def aremove_workspace_item(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantWorkspaceRemoveResult:
        assistant = await self.aget_assistant(assistant_id)
        if assistant is None:
            raise ValueError("assistant not found")
        workspace_root = self._assistant_workspace_root(assistant)
        current = self._find_workspace_item(
            assistant_id=assistant.assistant_id,
            workspace_root=workspace_root,
            kind=kind,
            slug=slug,
        )
        if current is None:
            raise ValueError(f"{kind[:-1]} not found in assistant workspace")
        self._remove_workspace_item_variants(
            workspace_root=workspace_root,
            kind=kind,
            slug=current.slug,
        )
        return AssistantWorkspaceRemoveResult(
            assistant_id=assistant.assistant_id,
            kind=kind,
            slug=current.slug,
            removed=True,
        )

    def reload_workspace_capability(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantRuntimeReloadResult:
        return run_sync(
            self.areload_workspace_capability(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
        )
            )

    async def areload_workspace_capability(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantRuntimeReloadResult:
        assistant = await self.aget_assistant(assistant_id)
        if assistant is None:
            raise ValueError("assistant not found")
        normalized_slug = slug.strip()
        if not normalized_slug:
            raise ValueError("capability slug cannot be empty")
        container_name = assistant.runtime.container_name
        if not container_name:
            return AssistantRuntimeReloadResult(
                assistant_id=assistant.assistant_id,
                kind=kind,
                slug=normalized_slug,
                status="skipped",
            )

        reload_method = getattr(self.provisioner, "reload_workspace_capability", None)
        try:
            if callable(reload_method):
                reloaded = await asyncio.to_thread(
                    reload_method,
                    assistant_id=assistant.assistant_id,
                    kind=kind,
                    slug=normalized_slug,
                    container_name=container_name,
                )
            elif kind == "skills" and hasattr(self.provisioner, "notify_workspace_skill_changed"):
                reloaded = await asyncio.to_thread(
                    self.provisioner.notify_workspace_skill_changed,
                    assistant_id=assistant.assistant_id,
                    skill_slug=normalized_slug,
                    container_name=container_name,
                )
            else:
                reloaded = True
        except Exception as exc:
            return AssistantRuntimeReloadResult(
                assistant_id=assistant.assistant_id,
                kind=kind,
                slug=normalized_slug,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        if not reloaded:
            return AssistantRuntimeReloadResult(
                assistant_id=assistant.assistant_id,
                kind=kind,
                slug=normalized_slug,
                status="failed",
                error="runtime command failed",
            )
        return AssistantRuntimeReloadResult(
            assistant_id=assistant.assistant_id,
            kind=kind,
            slug=normalized_slug,
            status="applied",
        )

    def delete_assistant(self, assistant_id: str) -> AssistantDeleteResult | None:
        return run_sync(self.adelete_assistant(assistant_id))

    async def adelete_assistant(self, assistant_id: str) -> AssistantDeleteResult | None:
        trimmed = assistant_id.strip()
        if not trimmed:
            return None

        existing = await self.aget_assistant(trimmed)
        if existing is None:
            return None

        try:
            destroy_result = self.provisioner.destroy_runtime(
                assistant_id=existing.assistant_id,
                workspace_root=None,
                state_root=None,
            )
            container_name = destroy_result.container_name
            container_removed = destroy_result.container_removed
            workspace_removed = False
            state_removed = False
        except Exception:
            # Docker may be unavailable on an admin workstation. Deleting the
            # assistant record should still be possible; runtime cleanup can be
            # retried separately when Docker is available again.
            container_name = existing.runtime.container_name or ""
            container_removed = False
            workspace_removed = False
            state_removed = False

        deleted_at = utc_now()
        runtime = existing.runtime.model_copy(
            update={
                "container_name": container_name or existing.runtime.container_name,
                "container_id": None,
                "status": "stopped",
                "gateway_host": None,
                "gateway_port": None,
                "gateway_url": None,
                "gateway_token": None,
                "last_error": "assistant soft deleted",
                "desired_state": "stopped",
                "observed_state": "stopped",
                "sync_state": "current",
                "last_observed_at": deleted_at,
                "last_reconciled_at": deleted_at,
                "last_runtime_error": "assistant soft deleted",
                "updated_at": deleted_at,
            }
        )
        deleted_record = existing.model_copy(
            update={
                "runtime": runtime,
                "updated_at": deleted_at,
                "deleted_at": deleted_at,
            }
        )
        await self.repository.aupsert_assistant(deleted_record)
        return AssistantDeleteResult(
            assistant_id=existing.assistant_id,
            container_name=container_name,
            container_removed=container_removed,
            workspace_removed=workspace_removed,
            state_removed=state_removed,
            soft_deleted=True,
            runtime_disabled=True,
            deleted_at=deleted_at,
        )

    def _runtime_needs_repair(
        self,
        record: AssistantRecord,
        probe: RuntimeProbeResult,
    ) -> bool:
        expected_image = get_settings().iron_agent_container_image.strip()
        if expected_image and (record.runtime.image or "").strip() != expected_image:
            return True
        if not probe.container_exists:
            return True
        if probe.status != "running":
            return True
        if record.runtime.status != probe.status:
            return True
        if record.runtime.container_id != probe.container_id:
            return True
        if record.runtime.gateway_url != probe.gateway_url:
            return True
        return False

    def _runtime_matches_probe(
        self,
        record: AssistantRecord,
        probe: RuntimeProbeResult,
    ) -> bool:
        runtime = record.runtime
        if self._runtime_has_pending_equivalent_probe(record, probe):
            return True
        expected_sync_state = sync_state_for_runtime(
            desired_state=runtime.desired_state,
            observed_state=probe.status,
            last_runtime_error=probe.last_error,
        )
        return (
            runtime.container_name == probe.container_name
            and runtime.container_id == probe.container_id
            and runtime.status == probe.status
            and runtime.observed_state == probe.status
            and runtime.sync_state == expected_sync_state
            and runtime.gateway_host == probe.gateway_host
            and runtime.gateway_port == probe.gateway_port
            and runtime.gateway_url == probe.gateway_url
            and runtime.gateway_token == self._gateway_token_for_assistant(record.assistant_id)
            and runtime.last_error == probe.last_error
            and runtime.last_runtime_error == probe.last_error
        )

    def _runtime_has_pending_equivalent_probe(
        self,
        record: AssistantRecord,
        probe: RuntimeProbeResult,
    ) -> bool:
        runtime = record.runtime
        if runtime.sync_state != "pending":
            return False
        if runtime.observed_state != probe.status:
            return False
        return (
            runtime.container_name == probe.container_name
            and runtime.container_id == probe.container_id
            and runtime.gateway_host == probe.gateway_host
            and runtime.gateway_port == probe.gateway_port
            and runtime.gateway_url == probe.gateway_url
            and runtime.last_runtime_error == probe.last_error
        )

    def _record_with_probe(
        self,
        record: AssistantRecord,
        probe: RuntimeProbeResult,
        *,
        desired_state: RuntimeStatusName | None = None,
    ) -> AssistantRecord:
        workspace_root = self._resolve_workspace_root_path(record)
        workspace_slug = normalize_workspace_slug(
            record.runtime.workspace_slug or record.workspace_config.workspace_slug,
            fallback=record.assistant_id,
        )
        now = utc_now()
        next_desired_state = desired_state or record.runtime.desired_state
        next_sync_state = sync_state_for_runtime(
            desired_state=next_desired_state,
            observed_state=probe.status,
            last_runtime_error=probe.last_error,
        )
        runtime = record.runtime.model_copy(
            update={
                "container_name": probe.container_name,
                "container_id": probe.container_id,
                "workspace_root": workspace_root,
                "workspace_root_id": record.runtime.workspace_root_id
                or record.workspace_config.root_id,
                "workspace_slug": workspace_slug,
                "state_root": self._runtime_state_root_for_sync(
                    record,
                    workspace_root=workspace_root,
                ),
                "status": probe.status,
                "gateway_host": probe.gateway_host,
                "gateway_port": probe.gateway_port,
                "gateway_url": probe.gateway_url,
                "gateway_token": self._gateway_token_for_assistant(record.assistant_id),
                "last_error": probe.last_error,
                "desired_state": next_desired_state,
                "observed_state": probe.status,
                "sync_state": next_sync_state,
                "last_observed_at": now,
                "last_reconciled_at": now,
                "last_runtime_error": probe.last_error,
                "updated_at": now,
            }
        )
        return record.model_copy(update={"runtime": runtime, "updated_at": now})

    @staticmethod
    def _gateway_token_for_assistant(assistant_id: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in assistant_id.strip())
        safe = "-".join(part for part in safe.split("-") if part)
        return f"ironagent-local-{safe or 'assistant'}"

    def _runtime_state_root_for_sync(
        self,
        record: AssistantRecord,
        *,
        workspace_root: str | None,
    ) -> str:
        settings = get_settings()
        if settings.runtime_backend == "kubernetes" and workspace_root:
            state_dir = settings.k8s_runtime_state_dir.strip().strip("/") or ".ironagent"
            return str((Path(workspace_root).expanduser() / state_dir).resolve(strict=False))
        return self._resolve_state_root_path(record)

    def upsert_assistant(
        self,
        *,
        assistant_id: str,
        name: str,
        capability_profile: CapabilityProfileName,
        notes: str = "",
        model_config: AssistantIronAgentConfig | None = None,
        workspace_config: AssistantWorkspaceConfig | None = None,
        provision_runtime: bool = True,
    ) -> AssistantRecord:
        return run_sync(
            self.aupsert_assistant(
                assistant_id=assistant_id,
                name=name,
                capability_profile=capability_profile,
                notes=notes,
                model_config=model_config,
                workspace_config=workspace_config,
                provision_runtime=provision_runtime,
        )
            )

    async def aupsert_assistant(
        self,
        *,
        assistant_id: str,
        name: str,
        capability_profile: CapabilityProfileName,
        notes: str = "",
        model_config: AssistantIronAgentConfig | None = None,
        workspace_config: AssistantWorkspaceConfig | None = None,
        provision_runtime: bool = True,
    ) -> AssistantRecord:
        trimmed_id = assistant_id.strip()
        trimmed_name = name.strip()
        if not trimmed_id:
            raise ValueError("assistant_id cannot be empty")
        if not trimmed_name:
            raise ValueError("name cannot be empty")

        now = utc_now()
        existing = await self.aget_assistant(trimmed_id)
        runtime = existing.runtime if existing else AssistantRuntimeRecord()
        effective_model_config = self._merge_model_config(
            existing.iron_agent_config if existing else None,
            model_config,
        )
        effective_workspace_config = self._merge_workspace_config(
            existing.workspace_config if existing else None,
            workspace_config,
            assistant_id=trimmed_id,
        )
        if existing is not None and existing.deleted:
            provision_runtime = False

        if provision_runtime:
            provisioned = self.provisioner.ensure_runtime(
                assistant_id=trimmed_id,
                assistant_name=trimmed_name,
                capability_profile=capability_profile,
                model_config=effective_model_config,
                workspace_config=effective_workspace_config,
            )
            desired_state = desired_state_for_status(provisioned.status)
            runtime = AssistantRuntimeRecord(
                container_name=provisioned.container_name,
                container_id=provisioned.container_id,
                workspace_root=provisioned.workspace_root,
                workspace_root_id=provisioned.workspace_root_id,
                workspace_slug=provisioned.workspace_slug,
                temp_dir=provisioned.temp_dir,
                persistent_dir=provisioned.persistent_dir,
                state_root=provisioned.state_root,
                status=provisioned.status,
                image=provisioned.image,
                gateway_host=provisioned.gateway_host,
                gateway_port=provisioned.gateway_port,
                gateway_url=provisioned.gateway_url,
                gateway_token=provisioned.gateway_token,
                last_error=provisioned.last_error,
                desired_state=desired_state,
                observed_state=provisioned.status,
                sync_state=sync_state_for_runtime(
                    desired_state=desired_state,
                    observed_state=provisioned.status,
                    last_runtime_error=provisioned.last_error,
                ),
                last_observed_at=provisioned.updated_at,
                last_reconciled_at=provisioned.updated_at,
                last_runtime_error=provisioned.last_error,
                updated_at=provisioned.updated_at,
            )
        updated = AssistantRecord(
            assistant_id=trimmed_id,
            name=trimmed_name,
            capability_profile=capability_profile,
            notes=notes.strip(),
            iron_agent_config=effective_model_config,
            workspace_config=effective_workspace_config,
            runtime=runtime,
            updated_at=now,
            deleted_at=existing.deleted_at if existing is not None else None,
        )

        if existing is not None:
            updated.created_at = existing.created_at
        await self.repository.aupsert_assistant(updated)
        return updated

    def _merge_model_config(
        self,
        existing: AssistantIronAgentConfig | None,
        incoming: AssistantIronAgentConfig | None,
    ) -> AssistantIronAgentConfig:
        base = (
            existing.model_copy(deep=True)
            if existing is not None
            else AssistantIronAgentConfig()
        )
        if incoming is None:
            return base

        base.auth_mode = incoming.auth_mode
        if base.auth_mode == "host_iron_agent":
            # Host-managed auth never carries provider credentials.
            base.provider_id = ""
            base.provider_base_url = ""
            base.provider_api_key = ""
        else:
            # provider_api_key mode: only overwrite a field when the caller
            # actually supplied it. A blank field means "keep the stored value",
            # so partial updates (e.g. rotating only the key) never silently drop
            # provider_id / base_url / the existing secret. A masked placeholder
            # echoed back from the UI is treated as "not supplied".
            if incoming.provider_id.strip():
                base.provider_id = incoming.provider_id.strip()
            if incoming.provider_base_url.strip():
                base.provider_base_url = incoming.provider_base_url.strip()
            next_api_key = incoming.provider_api_key.strip()
            if next_api_key and not is_placeholder_api_key(next_api_key):
                base.provider_api_key = next_api_key

        if incoming.primary_model.strip():
            base.primary_model = incoming.primary_model.strip()

        return base

    def _merge_workspace_config(
        self,
        existing: AssistantWorkspaceConfig | None,
        incoming: AssistantWorkspaceConfig | None,
        *,
        assistant_id: str,
    ) -> AssistantWorkspaceConfig:
        base = (
            existing.model_copy(deep=True)
            if existing is not None
            else AssistantWorkspaceConfig()
        )
        if incoming is not None:
            base.selection_mode = incoming.selection_mode
            next_root_id = incoming.root_id.strip()
            if next_root_id:
                base.root_id = next_root_id
            base.workspace_slug = incoming.workspace_slug.strip()
            base.auto_place_outputs = incoming.auto_place_outputs

        settings = get_settings()
        if not base.root_id.strip():
            base.root_id = settings.default_workspace_root_id.strip() or "default"

        # Validate the configured root and normalize the workspace slug before persisting.
        get_workspace_root(base.root_id, settings)
        base.workspace_slug = normalize_workspace_slug(
            base.workspace_slug,
            fallback=assistant_id,
        )
        return base

    def _resolve_workspace_root_path(self, record: AssistantRecord) -> str | None:
        configured_workspace_root = self._configured_workspace_root_path(record)
        if record.runtime.workspace_root:
            runtime_workspace_root = Path(record.runtime.workspace_root).expanduser()
            if record.runtime.workspace_root_id or record.runtime.workspace_slug:
                runtime_resolved = runtime_workspace_root.resolve(strict=False)
                configured_resolved = configured_workspace_root.resolve(strict=False)
                if (
                    runtime_resolved != configured_resolved
                    and not runtime_resolved.exists()
                    and configured_resolved.exists()
                    and self._looks_like_stale_local_runtime_path(runtime_resolved)
                ):
                    return str(configured_resolved)
                return str(runtime_resolved)
            if runtime_workspace_root.exists():
                runtime_resolved = runtime_workspace_root.resolve()
                if (
                    configured_workspace_root.exists()
                    and runtime_resolved != configured_workspace_root.resolve()
                ):
                    return str(configured_workspace_root.resolve())
                return str(runtime_resolved)
            if configured_workspace_root.exists():
                return str(configured_workspace_root.resolve())
            return str(runtime_workspace_root.resolve())
        return str(configured_workspace_root.resolve())

    def _configured_workspace_root_path(self, record: AssistantRecord) -> Path:
        settings = get_settings()
        workspace_root = get_workspace_root(record.workspace_config.root_id, settings)
        workspace_slug = normalize_workspace_slug(
            record.workspace_config.workspace_slug,
            fallback=record.assistant_id,
        )
        return (workspace_root.host_path / workspace_slug).resolve()

    def _resolve_state_root_path(self, record: AssistantRecord) -> str:
        settings = get_settings()
        configured_state_root = (
            settings.assistants_runtime_root / record.assistant_id / "state"
        ).resolve(strict=False)
        if record.runtime.state_root:
            runtime_state_root = Path(record.runtime.state_root).expanduser().resolve(strict=False)
            if (
                runtime_state_root != configured_state_root
                and not runtime_state_root.exists()
                and configured_state_root.exists()
                and self._looks_like_stale_local_runtime_path(runtime_state_root)
            ):
                return str(configured_state_root)
            return str(runtime_state_root)
        return str(configured_state_root)

    @staticmethod
    def _looks_like_stale_local_runtime_path(path: Path) -> bool:
        parts = path.parts
        return (
            (len(parts) > 1 and parts[1] == "Users")
            or "ironagent-main" in parts
            or "ironagent-feat-hooks" in parts
        )

    def _assistant_workspace_root(self, record: AssistantRecord) -> Path:
        workspace_root = self._resolve_workspace_root_path(record)
        if workspace_root is None:
            raise ValueError("assistant workspace is not provisioned")
        return Path(workspace_root).resolve()

    def _collect_workspace_items(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        root: Path,
        enabled: bool,
    ) -> list[AssistantWorkspaceItem]:
        if not root.exists():
            return []
        items: list[AssistantWorkspaceItem] = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir():
                continue
            if path.name.startswith(".") or not self._workspace_item_has_package_content(path):
                continue
            items.append(
                self._build_workspace_item(
                    assistant_id=assistant_id,
                    kind=kind,
                    path=path,
                    enabled=enabled,
            )
                )
        return items

    @staticmethod
    def _workspace_item_has_package_content(path: Path) -> bool:
        return (
            (path / "manifest.json").is_file()
            or (path / "ironagent.plugin.json").is_file()
            or (path / "SKILL.md").is_file()
        )

    def _build_workspace_item(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        path: Path,
        enabled: bool,
    ) -> AssistantWorkspaceItem:
        name = path.name
        version = ""
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            manifest_path = path / "ironagent.plugin.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
            if isinstance(manifest, dict):
                raw_name = manifest.get("name")
                raw_version = manifest.get("version")
                if isinstance(raw_name, str) and raw_name.strip():
                    name = raw_name.strip()
                if isinstance(raw_version, str) and raw_version.strip():
                    version = raw_version.strip()
        if not version:
            version = "-"
        return AssistantWorkspaceItem(
            assistant_id=assistant_id,
            kind=kind,
            slug=path.name,
            name=name,
            version=version,
            enabled=enabled,
            install_path=str(path.resolve()),
        )

    def _find_workspace_item(
        self,
        *,
        assistant_id: str,
        workspace_root: Path,
        kind: WorkspaceItemKind,
        slug: str,
    ) -> AssistantWorkspaceItem | None:
        candidates = [(workspace_root / kind / slug, True)]
        candidates.extend(
            (workspace_root / root / kind / slug, False) for root in DISABLED_WORKSPACE_ROOTS
        )
        for candidate, enabled in candidates:
            if candidate.exists() and candidate.is_dir():
                return self._build_workspace_item(
                    assistant_id=assistant_id,
                    kind=kind,
                    path=candidate,
                    enabled=enabled,
                )
        return None

    def _workspace_kind_root(
        self,
        *,
        workspace_root: Path,
        kind: WorkspaceItemKind,
        enabled: bool,
    ) -> Path:
        if enabled:
            return workspace_root / kind
        return workspace_root / DISABLED_WORKSPACE_ROOT / kind

    def _remove_path_if_exists(self, path: Path) -> None:
        if not path.exists():
            return
        if path.is_dir():
            rmtree(path)
            return
        path.unlink()

    def _remove_workspace_item_variants(
        self,
        *,
        workspace_root: Path,
        kind: WorkspaceItemKind,
        slug: str,
        keep_path: Path | None = None,
    ) -> None:
        resolved_keep = keep_path.resolve() if keep_path is not None else None
        candidates = [workspace_root / kind / slug]
        candidates.extend(workspace_root / root / kind / slug for root in DISABLED_WORKSPACE_ROOTS)
        for candidate in candidates:
            if resolved_keep is not None and candidate.resolve() == resolved_keep:
                continue
            self._remove_path_if_exists(candidate)
