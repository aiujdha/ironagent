from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.assistants.service import AssistantService
from src.model_registry.models import ModelRegistryRecord, utc_now
from src.runtime.models import AssistantIronAgentConfig
from src.runtime.provisioner import is_placeholder_api_key
from src.shared.config import get_settings
from src.shared.json_store import read_json_object, write_json_object


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug or "model"


class ModelRegistryService:
    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self.store_path = store_path or settings.model_registry_store_path

    def _load_models(self) -> list[ModelRegistryRecord]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("models", []) if isinstance(payload, dict) else []
        return [
            ModelRegistryRecord.model_validate(item)
            for item in items
            if isinstance(item, dict)
        ]

    def _write_models(self, items: list[ModelRegistryRecord]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_object(
            self.store_path,
            {
                "models": [
                    item.model_dump(mode="json")
                    for item in sorted(items, key=lambda item: item.name)
                ]
            },
        )

    def list_models(self) -> list[ModelRegistryRecord]:
        return self._load_models()

    def get_model(self, model_id: str) -> ModelRegistryRecord | None:
        trimmed = model_id.strip()
        if not trimmed:
            return None
        return next((item for item in self._load_models() if item.model_id == trimmed), None)

    def create_model(
        self,
        *,
        name: str,
        provider: str,
        url: str = "",
        api_key: str = "",
        status: str = "active",
    ) -> ModelRegistryRecord:
        trimmed_name = name.strip()
        trimmed_provider = provider.strip()
        if not trimmed_name:
            raise ValueError("name must not be empty")
        if not trimmed_provider:
            raise ValueError("provider must not be empty")
        if is_placeholder_api_key(api_key):
            raise ValueError("api_key must not be a masked placeholder ('***'/'******')")

        items = self._load_models()
        now = utc_now()
        candidate_id = _slugify(f"{trimmed_provider}-{trimmed_name}")
        next_id = candidate_id
        suffix = 2
        while any(item.model_id == next_id for item in items):
            next_id = f"{candidate_id}-{suffix}"
            suffix += 1

        record = ModelRegistryRecord(
            model_id=next_id,
            name=trimmed_name,
            provider=trimmed_provider,
            url=url.strip(),
            api_key=api_key.strip(),
            status=status.strip() or "active",
            created_at=now,
            updated_at=now,
        )
        items.append(record)
        self._write_models(items)
        return record

    def update_model(
        self,
        model_id: str,
        *,
        name: str,
        provider: str,
        url: str = "",
        api_key: str | None = None,
        status: str | None = None,
    ) -> ModelRegistryRecord | None:
        items = self._load_models()
        target = next((item for item in items if item.model_id == model_id), None)
        if target is None:
            return None

        target.name = name.strip() or target.name
        target.provider = provider.strip() or target.provider
        target.url = url.strip()
        if api_key is not None:
            trimmed_api_key = api_key.strip()
            if trimmed_api_key and trimmed_api_key != "******":
                target.api_key = trimmed_api_key
        if status is not None and status.strip():
            target.status = status.strip()
        target.updated_at = utc_now()
        self._write_models(items)
        return target

    def delete_model(self, model_id: str) -> bool:
        items = self._load_models()
        original_len = len(items)
        items = [item for item in items if item.model_id != model_id]
        if len(items) == original_len:
            return False
        self._write_models(items)
        return True

    def assistant_ids_for_model(
        self,
        model: ModelRegistryRecord,
        assistants: list[Any] | None = None,
    ) -> list[str]:
        service = AssistantService()
        records = assistants if assistants is not None else service.list_assistants()
        assistant_ids: list[str] = []
        for item in records:
            config = item.iron_agent_config
            if (
                config.primary_model.strip() == model.name
                and (config.provider_id or config.auth_mode).strip() == model.provider
                and config.provider_base_url.strip() == model.url
            ):
                assistant_ids.append(item.assistant_id)
        return assistant_ids

    def resolve_model_config(self, model_id: str) -> AssistantIronAgentConfig | None:
        item = self.get_model(model_id)
        if item is None:
            return None
        auth_mode = "provider_api_key"
        if item.provider == "host_iron_agent":
            auth_mode = "host_iron_agent"
        return AssistantIronAgentConfig(
            primary_model=item.name,
            auth_mode=auth_mode,
            provider_id="" if auth_mode == "host_iron_agent" else item.provider,
            provider_base_url="" if auth_mode == "host_iron_agent" else item.url,
            provider_api_key="" if auth_mode == "host_iron_agent" else item.api_key,
        )

    def probe_model(self, model_id: str) -> dict[str, Any] | None:
        item = self.get_model(model_id)
        if item is None:
            return None
        ok = bool(item.name.strip() and item.provider.strip())
        if ok and self._uses_host_openai_codex_auth(item):
            auth_result = self._probe_host_openai_codex_auth()
            if auth_result is not None:
                return {
                    "model_id": item.model_id,
                    **auth_result,
                }
            return {
                "model_id": item.model_id,
                "ok": True,
                "status": "configured",
                "message": "宿主机 OpenAI Codex 认证材料存在；OAuth 有效性以运行时真实调用为准",
            }
        return {
            "model_id": item.model_id,
            "ok": ok,
            "status": "reachable" if ok else "invalid",
            "message": "模型配置字段完整" if ok else "模型配置不完整",
        }

    def _uses_host_openai_codex_auth(self, item: ModelRegistryRecord) -> bool:
        return item.provider == "host_iron_agent" and item.name.strip().startswith("openai-codex/")

    def _probe_host_openai_codex_auth(self) -> dict[str, Any] | None:
        settings = get_settings()
        host_agent_root = (
            settings.host_iron_agent_root
            / "agents"
            / settings.host_iron_agent_agent_id
            / "agent"
        )
        auth_path = host_agent_root / "auth-profiles.json"
        models_path = host_agent_root / "models.json"
        if not auth_path.exists():
            return {
                "ok": False,
                "status": "auth_missing",
                "message": f"宿主机 OpenAI Codex 认证文件缺失：{auth_path}",
            }
        if not models_path.exists():
            return {
                "ok": False,
                "status": "auth_missing",
                "message": f"宿主机 OpenAI Codex 模型配置缺失：{models_path}",
            }
        payload = read_json_object(auth_path, None)
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "status": "auth_invalid",
                "message": f"宿主机 OpenAI Codex 认证文件不是有效 JSON：{auth_path}",
            }
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            return {
                "ok": False,
                "status": "auth_invalid",
                "message": f"宿主机 OpenAI Codex 认证文件没有 profiles：{auth_path}",
            }
        has_openai_codex = False
        for profile_id, profile in profiles.items():
            if str(profile_id).startswith("openai-codex"):
                has_openai_codex = True
                break
            if isinstance(profile, dict) and str(profile.get("provider", "")) == "openai-codex":
                has_openai_codex = True
                break
        if not has_openai_codex:
            return {
                "ok": False,
                "status": "auth_invalid",
                "message": f"宿主机认证文件未包含 openai-codex profile：{auth_path}",
            }
        return None

    def import_from_assistants(
        self,
        assistants: list[Any] | None = None,
    ) -> list[ModelRegistryRecord]:
        service = AssistantService()
        records = assistants if assistants is not None else service.list_assistants()
        items = self._load_models()
        changed = False
        now = utc_now()
        for assistant in records:
            config = assistant.iron_agent_config
            name = config.primary_model.strip()
            provider = (config.provider_id or config.auth_mode).strip()
            url = config.provider_base_url.strip()
            api_key = config.provider_api_key.strip()
            if is_placeholder_api_key(api_key):
                # A placeholder ("***"/"******") is not a real credential; never
                # import it into the registry as a usable key.
                api_key = ""
            if not name or not provider:
                continue
            exists = next(
                (
                    item
                    for item in items
                    if item.name == name and item.provider == provider and item.url == url
                ),
                None,
            )
            if exists is not None:
                if api_key and not exists.api_key:
                    exists.api_key = api_key
                    exists.updated_at = now
                    changed = True
                continue
            candidate_id = _slugify(f"{provider}-{name}")
            next_id = candidate_id
            suffix = 2
            while any(item.model_id == next_id for item in items):
                next_id = f"{candidate_id}-{suffix}"
                suffix += 1
            items.append(
                ModelRegistryRecord(
                    model_id=next_id,
                    name=name,
                    provider=provider,
                    url=url,
                    api_key=api_key,
                    status="active",
                    created_at=now,
                    updated_at=now,
            )
                )
            changed = True
        if changed:
            self._write_models(items)
        return items
