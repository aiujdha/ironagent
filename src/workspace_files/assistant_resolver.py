"""Resolve workspace file operations to a concrete assistant."""

from __future__ import annotations

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.users.service import UserService


class AssistantResolver:
    def __init__(
        self,
        *,
        assistant_service: AssistantService,
        user_service: UserService,
    ) -> None:
        self.assistant_service = assistant_service
        self.user_service = user_service

    def resolve_assistant_id(
        self,
        *,
        assistant_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        if assistant_id:
            return assistant_id.strip()
        if user_id:
            resolved = self.user_service.resolve_primary_assistant(user_id.strip())
            if resolved is None:
                raise ValueError(f"no assistant bound to user {user_id!r}")
            return resolved
        raise ValueError("either assistant_id or user_id must be provided")

    def require_assistant(self, assistant_id: str) -> AssistantRecord:
        assistant = self.assistant_service.get_assistant(assistant_id)
        if assistant is None:
            raise ValueError(f"assistant {assistant_id!r} does not exist")
        return assistant