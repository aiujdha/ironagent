from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from src.runtime.models import (
    AssistantIronAgentConfig,
    AssistantRuntimeRecord,
    AssistantWorkspaceConfig,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


DEFAULT_CAPABILITY_PROFILES = ("chat", "onlyread", "onlywrite", "work_no_exec", "dangerous")


def _capability_profile_sort_key(profile_id: str) -> tuple[int, str]:
    try:
        return (DEFAULT_CAPABILITY_PROFILES.index(profile_id), profile_id)
    except ValueError:
        return (len(DEFAULT_CAPABILITY_PROFILES), profile_id)


def _load_available_profiles() -> tuple[str, ...]:
    profiles_dir = Path(__file__).parent.parent.parent / "config" / "capabilities"
    if not profiles_dir.exists():
        return DEFAULT_CAPABILITY_PROFILES
    return tuple(
        p.stem
        for p in sorted(
            profiles_dir.glob("*.yaml"),
            key=lambda path: _capability_profile_sort_key(path.stem),
    )
        )


CapabilityProfileName = str
CAPABILITY_PROFILES = _load_available_profiles()


class AssistantRecord(BaseModel):
    assistant_id: str
    name: str
    capability_profile: CapabilityProfileName = "onlyread"
    notes: str = ""
    iron_agent_config: AssistantIronAgentConfig = Field(default_factory=AssistantIronAgentConfig)
    workspace_config: AssistantWorkspaceConfig = Field(default_factory=AssistantWorkspaceConfig)
    runtime: AssistantRuntimeRecord = Field(default_factory=AssistantRuntimeRecord)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None
