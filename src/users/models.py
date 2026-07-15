"""Data models for enterprise users and user-assistant bindings."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


UserRole = Literal["user", "admin", "developer"]


class User(BaseModel):
    """Enterprise user record."""

    user_id: str
    display_name: str = ""
    employee_id: str = ""
    department: str = ""
    role: UserRole = "user"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserAssistantBinding(BaseModel):
    """Maps an enterprise user to an assistant instance."""

    user_id: str
    assistant_id: str
    alias: str = ""
    source_system: str = ""
    bound_at: datetime = Field(default_factory=utc_now)
