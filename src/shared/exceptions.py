class IronAgentError(Exception):
    """Base exception for IronAgent."""


class ConfigurationError(IronAgentError):
    """Raised when a required configuration file is invalid."""


class ApprovalTimeoutError(IronAgentError):
    """Raised when an approval request expires."""
