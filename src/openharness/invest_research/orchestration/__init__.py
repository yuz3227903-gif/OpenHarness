"""CrewAI orchestration for the investment-research OpenHarness runtime.

CrewAI owns workflow control only.  Agent reasoning, model calls, tools,
permissions, and output validation remain inside OpenHarness.
"""

from __future__ import annotations

import os
from pathlib import Path


def configure_crewai_environment(project_root: str | Path | None = None) -> Path:
    """Keep CrewAI state project-local and disable outbound telemetry by default."""

    default_root = Path(__file__).resolve().parents[4]
    root = Path(project_root or default_root).resolve()
    storage = root / ".openharness" / "data" / "crewai"
    storage.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CREWAI_STORAGE_DIR", str(storage))
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    return storage


__all__ = ["configure_crewai_environment"]
