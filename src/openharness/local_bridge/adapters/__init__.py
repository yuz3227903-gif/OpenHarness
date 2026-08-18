"""Local Agent adapters."""

from openharness.local_bridge.adapters.base import (
    AgentCapabilities,
    DetectionResult,
    LocalAgentAdapter,
)
from openharness.local_bridge.adapters.registry import (
    available_providers,
    detect_all,
    get_adapter,
    register_adapter,
)

__all__ = [
    "AgentCapabilities",
    "DetectionResult",
    "LocalAgentAdapter",
    "available_providers",
    "detect_all",
    "get_adapter",
    "register_adapter",
]
