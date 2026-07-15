from src.security.capability import CapabilityProfile


def tool_is_allowed(profile: CapabilityProfile, tool_name: str) -> bool:
    return tool_name in profile.allowed_tools


def tool_requires_approval(profile: CapabilityProfile, tool_name: str) -> bool:
    return tool_name in profile.approval_tools
