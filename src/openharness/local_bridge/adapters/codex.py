"""Codex CLI."""

from __future__ import annotations

import re

from openharness.local_bridge.adapters.base import AgentCapabilities, LocalAgentAdapter

_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?[\w.-]*")


class CodexAdapter(LocalAgentAdapter):
    provider = "codex"
    display_name = "Codex"
    # Windows installs expose a .cmd shim; shutil.which resolves either name.
    executables = ("codex",)

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            chat=True,
            streaming=True,
            shell=True,
            file_read=True,
            file_write=True,
            diff=True,
            mcp=True,
            skills=False,
            resume_session=True,
            approval=True,
        )

    def parse_version(self, output: str) -> str | None:
        """Codex prints a banner such as ``codex-cli 0.5.1``.

        Pull just the number so the UI shows a version rather than a sentence.
        """

        match = _VERSION.search(output)
        if match:
            return match.group(0)
        return super().parse_version(output)


__all__ = ["CodexAdapter"]
