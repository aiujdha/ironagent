from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PlacementTarget = Literal["temp", "persistent"]


@dataclass(slots=True)
class WorkspacePlacementResult:
    tool_args: dict[str, Any]
    metadata: dict[str, object]
