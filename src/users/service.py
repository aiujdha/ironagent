"""Service layer for enterprise users and user-assistant bindings."""

from __future__ import annotations

from pathlib import Path

from src.shared.async_utils import run_sync
from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.users.models import User, UserAssistantBinding, UserRole, utc_now
from src.users.repository import (
    JsonUserBindingRepository,
    JsonUserRepository,
    UserBindingRepository,
    UserRepository,
    build_user_binding_repository,
    build_user_repository,
)

log = get_logger(__name__)


class UserService:
    def __init__(
        self,
        store_path: Path | None = None,
        bindings_path: Path | None = None,
        repository: UserRepository | None = None,
        binding_repository: UserBindingRepository | None = None,
    ) -> None:
        settings = get_settings()
        explicit_store_path = store_path
        explicit_bindings_path = bindings_path
        self.store_path = explicit_store_path or settings.users_store_path
        self.bindings_path = explicit_bindings_path or settings.user_bindings_store_path
        if repository is not None:
            self.repository = repository
        elif explicit_store_path is not None:
            self.repository = JsonUserRepository(self.store_path)
        else:
            self.repository = build_user_repository()
        if binding_repository is not None:
            self.binding_repository = binding_repository
        elif explicit_bindings_path is not None:
            self.binding_repository = JsonUserBindingRepository(self.bindings_path)
        else:
            self.binding_repository = build_user_binding_repository()

    def list_users(self) -> list[User]:
        return run_sync(self.alist_users())

    async def alist_users(self) -> list[User]:
        return await self.repository.alist_users()

    def get_user(self, user_id: str) -> User | None:
        return run_sync(self.aget_user(user_id))

    async def aget_user(self, user_id: str) -> User | None:
        return await self.repository.aget_user(user_id)

    def create_user(
        self,
        user_id: str,
        *,
        display_name: str = "",
        employee_id: str = "",
        department: str = "",
        role: UserRole = "user",
    ) -> User:
        return run_sync(
            self.acreate_user(
                user_id,
                display_name=display_name,
                employee_id=employee_id,
                department=department,
                role=role,
        )
            )

    async def acreate_user(
        self,
        user_id: str,
        *,
        display_name: str = "",
        employee_id: str = "",
        department: str = "",
        role: UserRole = "user",
    ) -> User:
        trimmed_user_id = user_id.strip()
        if not trimmed_user_id:
            raise ValueError("user_id must not be empty")
        if await self.repository.aget_user(trimmed_user_id) is not None:
            raise ValueError(f"user {trimmed_user_id!r} already exists")
        now = utc_now()
        user = User(
            user_id=trimmed_user_id,
            display_name=display_name,
            employee_id=employee_id,
            department=department,
            role=role,
            created_at=now,
            updated_at=now,
        )
        await self.repository.aupsert_user(user)
        log.info("user_created", user_id=trimmed_user_id)
        return user

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        employee_id: str | None = None,
        department: str | None = None,
        role: UserRole | None = None,
    ) -> User | None:
        return run_sync(
            self.aupdate_user(
                user_id,
                display_name=display_name,
                employee_id=employee_id,
                department=department,
                role=role,
        )
            )

    async def aupdate_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        employee_id: str | None = None,
        department: str | None = None,
        role: UserRole | None = None,
    ) -> User | None:
        target = await self.repository.aget_user(user_id)
        if target is None:
            return None
        if display_name is not None:
            target.display_name = display_name
        if employee_id is not None:
            target.employee_id = employee_id
        if department is not None:
            target.department = department
        if role is not None:
            target.role = role
        target.updated_at = utc_now()
        await self.repository.aupsert_user(target)
        log.info("user_updated", user_id=user_id)
        return target

    def delete_user(self, user_id: str) -> bool:
        return run_sync(self.adelete_user(user_id))

    async def adelete_user(self, user_id: str) -> bool:
        if await self.repository.aget_user(user_id) is None:
            return False
        await self.binding_repository.adelete_bindings_for_user(user_id)
        deleted = await self.repository.adelete_user(user_id)
        log.info("user_deleted", user_id=user_id)
        return deleted

    def bind_assistant(
        self,
        user_id: str,
        assistant_id: str,
        *,
        alias: str = "",
        source_system: str = "",
    ) -> UserAssistantBinding:
        return run_sync(
            self.abind_assistant(
                user_id,
                assistant_id,
                alias=alias,
                source_system=source_system,
        )
            )

    async def abind_assistant(
        self,
        user_id: str,
        assistant_id: str,
        *,
        alias: str = "",
        source_system: str = "",
    ) -> UserAssistantBinding:
        trimmed_user_id = user_id.strip()
        trimmed_assistant_id = assistant_id.strip()
        if not trimmed_user_id or not trimmed_assistant_id:
            raise ValueError("user_id and assistant_id must not be empty")
        bindings = await self.binding_repository.alist_bindings()
        existing = next(
            (
                item
                for item in bindings
                if item.user_id == trimmed_user_id and item.assistant_id == trimmed_assistant_id
            ),
            None,
        )
        if existing is not None:
            existing.alias = alias or existing.alias
            existing.source_system = source_system or existing.source_system
            await self.binding_repository.aupsert_binding(existing)
            log.info("binding_updated", user_id=trimmed_user_id, assistant_id=trimmed_assistant_id)
            return existing

        binding = UserAssistantBinding(
            user_id=trimmed_user_id,
            assistant_id=trimmed_assistant_id,
            alias=alias,
            source_system=source_system,
        )
        await self.binding_repository.aupsert_binding(binding)
        log.info("binding_created", user_id=trimmed_user_id, assistant_id=trimmed_assistant_id)
        return binding

    def unbind_assistant(self, user_id: str, assistant_id: str) -> bool:
        return run_sync(self.aunbind_assistant(user_id, assistant_id))

    async def aunbind_assistant(self, user_id: str, assistant_id: str) -> bool:
        removed = await self.binding_repository.adelete_binding(user_id, assistant_id)
        if removed:
            log.info("binding_removed", user_id=user_id, assistant_id=assistant_id)
        return removed

    def list_bindings_for_user(self, user_id: str) -> list[UserAssistantBinding]:
        return run_sync(self.alist_bindings_for_user(user_id))

    def list_bindings_grouped_by_user(self) -> dict[str, list[UserAssistantBinding]]:
        return run_sync(self.alist_bindings_grouped_by_user())

    async def alist_bindings_grouped_by_user(self) -> dict[str, list[UserAssistantBinding]]:
        grouped: dict[str, list[UserAssistantBinding]] = {}
        for item in await self.binding_repository.alist_bindings():
            grouped.setdefault(item.user_id, []).append(item)
        return grouped

    async def alist_bindings_for_user(self, user_id: str) -> list[UserAssistantBinding]:
        return [
            item
            for item in await self.binding_repository.alist_bindings()
            if item.user_id == user_id
        ]

    def list_bindings_for_assistant(self, assistant_id: str) -> list[UserAssistantBinding]:
        return run_sync(self.alist_bindings_for_assistant(assistant_id))

    async def alist_bindings_for_assistant(self, assistant_id: str) -> list[UserAssistantBinding]:
        return [
            item
            for item in await self.binding_repository.alist_bindings()
            if item.assistant_id == assistant_id
        ]

    def resolve_assistant_ids(self, user_id: str) -> list[str]:
        return [item.assistant_id for item in self.list_bindings_for_user(user_id)]

    def resolve_primary_assistant(self, user_id: str) -> str | None:
        bindings = self.list_bindings_for_user(user_id)
        if not bindings:
            return None
        return min(bindings, key=lambda item: item.bound_at).assistant_id
