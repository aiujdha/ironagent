from __future__ import annotations

from src.shared.config import Settings, get_settings

from .kubernetes import K8sRuntimeProvisioner
from .provisioner import AssistantRuntimeProvisioner, CommandRunner


def build_runtime_provisioner(
    *,
    settings: Settings | None = None,
    command_runner: CommandRunner | None = None,
) -> AssistantRuntimeProvisioner:
    resolved_settings = settings or get_settings()
    if resolved_settings.runtime_backend == "kubernetes":
        return K8sRuntimeProvisioner(
            settings=resolved_settings,
            command_runner=command_runner,
        )
    return AssistantRuntimeProvisioner(
        settings=resolved_settings,
        command_runner=command_runner,
    )
