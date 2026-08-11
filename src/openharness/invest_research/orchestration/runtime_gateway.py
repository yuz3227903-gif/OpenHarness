"""Narrow bridge from CrewAI Flow steps to the OpenHarness Agent runtime."""

from __future__ import annotations

from typing import Any, Protocol

from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    AgentExecutionResult,
    InvestmentResearchRuntimeAdapter,
)


class AgentRuntime(Protocol):
    async def execute_agent(self, request: AgentExecutionRequest) -> AgentExecutionResult: ...


class OpenHarnessRuntimeGateway:
    """Call exactly one OpenHarness Agent for each CrewAI workflow step."""

    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        self._runtime = runtime or InvestmentResearchRuntimeAdapter()
        self.execution_count = 0

    async def execute(
        self,
        *,
        agent_id: str,
        input_payload: dict[str, Any],
        task_prompt: str,
        context_package: dict[str, Any] | None = None,
        max_turns: int | None = None,
        tool_call_limits: dict[str, int] | None = None,
    ) -> AgentExecutionResult:
        """Forward one typed request without adding a CrewAI model call."""

        self.execution_count += 1
        return await self._runtime.execute_agent(
            AgentExecutionRequest(
                agent_id=agent_id,
                input_payload=input_payload,
                task_prompt=task_prompt,
                context_package=context_package or {},
                max_turns=max_turns,
                tool_call_limits=tool_call_limits or {},
            )
        )


__all__ = ["AgentRuntime", "OpenHarnessRuntimeGateway"]
