"""OpenClaw CLI."""

from __future__ import annotations

import re

from openharness.local_bridge.adapters.base import AgentCapabilities, LocalAgentAdapter

_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?[\w.-]*")


class OpenClawAdapter(LocalAgentAdapter):
    provider = "openclaw"
    display_name = "OpenClaw"
    executables = ("openclaw",)

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            chat=True,
            streaming=True,
            shell=True,
            file_read=True,
            file_write=True,
            diff=True,
            mcp=True,
            skills=True,
            resume_session=True,
            approval=True,
        )

    def parse_version(self, output: str) -> str | None:
        match = _VERSION.search(output)
        if match:
            return match.group(0)
        return super().parse_version(output)


__all__ = ["OpenClawAdapter"]
