from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class CapabilityProfile:
    name: str
    level: int
    allowed_tools: set[str]
    approval_tools: set[str]
    metadata: dict[str, Any]


def load_capability_profile(path: Path) -> CapabilityProfile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return CapabilityProfile(
        name=raw.get("name", path.stem),
        level=int(raw.get("level", 0)),
        allowed_tools=set(raw.get("allowed_tools", [])),
        approval_tools=set(raw.get("approval_tools", [])),
        metadata={
            k: v
            for k, v in raw.items()
            if k not in {"name", "level", "allowed_tools", "approval_tools"}
        },
    )
