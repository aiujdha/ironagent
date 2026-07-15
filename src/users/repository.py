from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import DateTime, Index, String, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.config import StorageBackendName, get_settings
from src.shared.database import Base, get_session_factory
from src.shared.json_store import read_json_object, write_json_object
from src.users.models import User, UserAssistantBinding


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserRepository(Protocol):
    async def alist_users(self) -> list[User]: ...

    async def aget_user(self, user_id: str) -> User | None: ...

    async def aupsert_user(self, user: User) -> User: ...

    async def adelete_user(self, user_id: str) -> bool: ...


class UserBindingRepository(Protocol):
    async def alist_bindings(self) -> list[UserAssistantBinding]: ...

    async def aupsert_binding(self, binding: UserAssistantBinding) -> UserAssistantBinding: ...

    async def adelete_binding(self, user_id: str, assistant_id: str) -> bool: ...

    async def adelete_bindings_for_user(self, user_id: str) -> int: ...


class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    employee_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    department: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserAssistantBindingRow(Base):
    __tablename__ = "user_assistant_bindings"
    __table_args__ = (Index("ix_user_assistant_bindings_assistant_id", "assistant_id"),)

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    assistant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_system: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JsonUserRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    async def alist_users(self) -> list[User]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("users", []) if isinstance(payload, dict) else []
        return [User.model_validate(item) for item in items if isinstance(item, dict)]

    async def aget_user(self, user_id: str) -> User | None:
        trimmed = user_id.strip()
        if not trimmed:
            return None
        for user in await self.alist_users():
            if user.user_id == trimmed:
                return user
        return None

    async def aupsert_user(self, user: User) -> User:
        users = await self.alist_users()
        replaced = False
        next_users: list[User] = []
        for existing in users:
            if existing.user_id == user.user_id:
                next_users.append(user)
                replaced = True
            else:
                next_users.append(existing)
        if not replaced:
            next_users.append(user)
        self._write_users(next_users)
        return user

    async def adelete_user(self, user_id: str) -> bool:
        trimmed = user_id.strip()
        if not trimmed:
            return False
        users = await self.alist_users()
        next_users = [user for user in users if user.user_id != trimmed]
        if len(next_users) == len(users):
            return False
        self._write_users(next_users)
        return True

    def _write_users(self, users: Sequence[User]) -> None:
        write_json_object(
            self.store_path,
            {
                "users": [
                    item.model_dump(mode="json")
                    for item in sorted(users, key=lambda user: user.user_id)
                ]
            },
        )


class JsonUserBindingRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    async def alist_bindings(self) -> list[UserAssistantBinding]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("bindings", []) if isinstance(payload, dict) else []
        return [
            UserAssistantBinding.model_validate(item) for item in items if isinstance(item, dict)
        ]

    async def aupsert_binding(self, binding: UserAssistantBinding) -> UserAssistantBinding:
        bindings = await self.alist_bindings()
        replaced = False
        next_bindings: list[UserAssistantBinding] = []
        for existing in bindings:
            if (
                existing.user_id == binding.user_id
                and existing.assistant_id == binding.assistant_id
            ):
                next_bindings.append(binding)
                replaced = True
            else:
                next_bindings.append(existing)
        if not replaced:
            next_bindings.append(binding)
        self._write_bindings(next_bindings)
        return binding

    async def adelete_binding(self, user_id: str, assistant_id: str) -> bool:
        bindings = await self.alist_bindings()
        next_bindings = [
            item
            for item in bindings
            if not (item.user_id == user_id and item.assistant_id == assistant_id)
        ]
        if len(next_bindings) == len(bindings):
            return False
        self._write_bindings(next_bindings)
        return True

    async def adelete_bindings_for_user(self, user_id: str) -> int:
        bindings = await self.alist_bindings()
        next_bindings = [item for item in bindings if item.user_id != user_id]
        removed = len(bindings) - len(next_bindings)
        if removed:
            self._write_bindings(next_bindings)
        return removed

    def _write_bindings(self, bindings: Sequence[UserAssistantBinding]) -> None:
        write_json_object(
            self.store_path,
            {
                "bindings": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        bindings,
                        key=lambda binding: (binding.user_id, binding.assistant_id),
                    )
                ]
            },
        )


class DatabaseUserRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory
        self._role_column_ready = False

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    async def _ensure_role_column(self, session: AsyncSession) -> None:
        if self._role_column_ready:
            return
        columns = await session.run_sync(
            lambda sync_session: {
                column["name"] for column in inspect(sync_session.bind).get_columns("users")
            }
        )
        if "role" in columns:
            self._role_column_ready = True
            return
        dialect_name = session.bind.dialect.name if session.bind is not None else ""
        if dialect_name == "mysql":
            await session.execute(
                text("ALTER TABLE users ADD COLUMN role VARCHAR(64) NOT NULL DEFAULT 'user'")
            )
            await session.commit()
        self._role_column_ready = True

    async def alist_users(self) -> list[User]:
        async with self.session_factory()() as session:
            await self._ensure_role_column(session)
            result = await session.execute(select(UserRow).order_by(UserRow.user_id.asc()))
            return [_user_from_row(row) for row in result.scalars().all()]

    async def aget_user(self, user_id: str) -> User | None:
        trimmed = user_id.strip()
        if not trimmed:
            return None
        async with self.session_factory()() as session:
            await self._ensure_role_column(session)
            row = await session.get(UserRow, trimmed)
            return None if row is None else _user_from_row(row)

    async def aupsert_user(self, user: User) -> User:
        async with self.session_factory()() as session:
            await self._ensure_role_column(session)
            row = await session.get(UserRow, user.user_id)
            if row is None:
                row = UserRow(user_id=user.user_id)
                session.add(row)
            _apply_user_to_row(row, user)
            await session.commit()
        return user

    async def adelete_user(self, user_id: str) -> bool:
        trimmed = user_id.strip()
        if not trimmed:
            return False
        async with self.session_factory()() as session:
            await self._ensure_role_column(session)
            row = await session.get(UserRow, trimmed)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


class DatabaseUserBindingRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    async def alist_bindings(self) -> list[UserAssistantBinding]:
        async with self.session_factory()() as session:
            result = await session.execute(
                select(UserAssistantBindingRow).order_by(
                    UserAssistantBindingRow.user_id.asc(),
                    UserAssistantBindingRow.assistant_id.asc(),
            )
                )
            return [_binding_from_row(row) for row in result.scalars().all()]

    async def aupsert_binding(self, binding: UserAssistantBinding) -> UserAssistantBinding:
        async with self.session_factory()() as session:
            row = await session.get(
                UserAssistantBindingRow,
                {"user_id": binding.user_id, "assistant_id": binding.assistant_id},
            )
            if row is None:
                row = UserAssistantBindingRow(
                    user_id=binding.user_id,
                    assistant_id=binding.assistant_id,
                )
                session.add(row)
            _apply_binding_to_row(row, binding)
            await session.commit()
        return binding

    async def adelete_binding(self, user_id: str, assistant_id: str) -> bool:
        async with self.session_factory()() as session:
            row = await session.get(
                UserAssistantBindingRow,
                {"user_id": user_id, "assistant_id": assistant_id},
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def adelete_bindings_for_user(self, user_id: str) -> int:
        async with self.session_factory()() as session:
            result = await session.execute(
                select(UserAssistantBindingRow).where(UserAssistantBindingRow.user_id == user_id)
            )
            rows = result.scalars().all()
            for row in rows:
                await session.delete(row)
            if rows:
                await session.commit()
            return len(rows)


def build_user_repository(
    *,
    backend: StorageBackendName | None = None,
) -> UserRepository:
    settings = get_settings()
    effective_backend = backend or settings.storage_backend
    if effective_backend == "mysql":
        return DatabaseUserRepository()
    return JsonUserRepository(settings.users_store_path)


def build_user_binding_repository(
    *,
    backend: StorageBackendName | None = None,
) -> UserBindingRepository:
    settings = get_settings()
    effective_backend = backend or settings.storage_backend
    if effective_backend == "mysql":
        return DatabaseUserBindingRepository()
    return JsonUserBindingRepository(settings.user_bindings_store_path)


def _user_from_row(row: UserRow) -> User:
    return User.model_validate(
        {
            "user_id": row.user_id,
            "display_name": row.display_name,
            "employee_id": row.employee_id,
            "department": row.department,
            "role": row.role,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _apply_user_to_row(row: UserRow, user: User) -> None:
    row.display_name = user.display_name
    row.employee_id = user.employee_id
    row.department = user.department
    row.role = user.role
    row.created_at = user.created_at
    row.updated_at = user.updated_at


def _binding_from_row(row: UserAssistantBindingRow) -> UserAssistantBinding:
    return UserAssistantBinding.model_validate(
        {
            "user_id": row.user_id,
            "assistant_id": row.assistant_id,
            "alias": row.alias,
            "source_system": row.source_system,
            "bound_at": row.bound_at,
        }
    )


def _apply_binding_to_row(
    row: UserAssistantBindingRow,
    binding: UserAssistantBinding,
) -> None:
    row.alias = binding.alias
    row.source_system = binding.source_system
    row.bound_at = binding.bound_at
