"""Where adapters are looked up.

Detection is a fan-out over whatever is registered here, so adding an Agent
means adding an adapter and one registration line — never an ``if provider ==``
inside a request handler.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from openharness.local_bridge.adapters.base import DetectionResult, LocalAgentAdapter
from openharness.local_bridge.adapters.codex import CodexAdapter
from openharness.local_bridge.adapters.openclaw import OpenClawAdapter

#: Adapters shipped today. The interface is designed for Claude Code, Gemini
#: CLI, OpenCode and ACP to land as further entries without touching callers.
_ADAPTER_CLASSES: tuple[type[LocalAgentAdapter], ...] = (
    CodexAdapter,
    OpenClawAdapter,
)

_REGISTRY: dict[str, LocalAgentAdapter] = {}


def _registry() -> dict[str, LocalAgentAdapter]:
    if not _REGISTRY:
        for adapter_class in _ADAPTER_CLASSES:
            adapter = adapter_class()
            _REGISTRY[adapter.provider] = adapter
    return _REGISTRY


def register_adapter(adapter: LocalAgentAdapter) -> None:
    """Add an adapter at runtime, for plugins and for tests."""

    _registry()[adapter.provider] = adapter


def available_providers() -> list[str]:
    return sorted(_registry())


def get_adapter(provider: str) -> LocalAgentAdapter | None:
    return _registry().get(provider)


def detect_all(*, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Probe every known Agent.

    Probes run in parallel: each one may wait on a process, and a user with
    several Agents installed should not wait for the sum of them. One adapter
    raising is reported against that adapter alone, never as a failed sweep.
    """

    adapters = list(_registry().values())
    if not adapters:
        return []

    def probe(adapter: LocalAgentAdapter) -> DetectionResult:
        try:
            return adapter.detect()
        except Exception as exc:  # noqa: BLE001 - one bad adapter must not hide the rest
            return DetectionResult(
                provider=adapter.provider,
                display_name=adapter.display_name,
                installed=False,
                status="error",
                detail=f"检测适配器异常：{type(exc).__name__}: {exc}",
                capabilities={},
            )

    with ThreadPoolExecutor(max_workers=min(8, len(adapters))) as pool:
        results = list(pool.map(probe, adapters, timeout=timeout))
    return [result.to_dict() for result in sorted(results, key=lambda item: item.provider)]


__all__ = [
    "available_providers",
    "detect_all",
    "get_adapter",
    "register_adapter",
]
