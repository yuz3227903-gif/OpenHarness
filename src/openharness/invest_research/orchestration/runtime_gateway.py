"""Narrow bridge from CrewAI Flow steps to the OpenHarness Agent runtime."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, Protocol

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

    @property
    def evidence_store(self):
        return getattr(self._runtime, "evidence_store", None)

    async def execute(
        self,
        *,
        agent_id: str,
        input_payload: dict[str, Any],
        task_prompt: str,
        context_package: dict[str, Any] | None = None,
        max_turns: int | None = None,
        max_output_tokens: int | None = None,
        tool_call_limits: dict[str, int] | None = None,
        timeout_seconds: float = 300,
        retry_transient: bool = True,
        output_contract: Literal["agent_default", "report_section"] = "agent_default",
        persist_output: bool = True,
    ) -> AgentExecutionResult:
        """Forward one typed request without adding a CrewAI model call."""

        request = AgentExecutionRequest(
            agent_id=agent_id,
            input_payload=input_payload,
            task_prompt=task_prompt,
            context_package=context_package or {},
            max_turns=max_turns,
            max_output_tokens=max_output_tokens,
            tool_call_limits=tool_call_limits or {},
            timeout_seconds=timeout_seconds,
            output_contract=output_contract,
            persist_output=persist_output,
        )
        max_attempts = 2 if retry_transient else 1
        for attempt in range(max_attempts):
            self.execution_count += 1
            try:
                result = await self._runtime.execute_agent(request)
            except Exception as exc:
                result = AgentExecutionResult(
                    status="failed",
                    agent_id=agent_id,
                    runtime_agent_name=f"investment-research:{agent_id}",
                    failure_class=_classify_exception(exc),
                    retry_count=attempt,
                    degraded=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if not _retryable_result(result) or attempt == max_attempts - 1:
                if attempt:
                    result.retry_count += attempt
                    result.degraded = True
                    if result.failure_class == "empty_response":
                        result.warnings.append(
                            f"{agent_id} empty-response recovery retried once in a fresh runtime session."
                        )
                    else:
                        result.warnings.append(
                            f"{agent_id} runtime retry attempts: {attempt}."
                        )
                return result
            await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("Agent runtime did not return a result")


def _retryable_result(result: AgentExecutionResult) -> bool:
    return result.status == "failed" and result.failure_class in {
        "network",
        "rate_limit",
        "timeout",
        "empty_response",
    }


def _classify_exception(exc: Exception) -> str:
    text = str(exc).lower()
    if "401" in text or "403" in text or "unauthorized" in text:
        return "provider_auth"
    if "429" in text or "rate limit" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text or "execution exceeded" in text:
        return "timeout"
    if any(marker in text for marker in ("network", "connection", "httpx")):
        return "network"
    if "permission" in text or "not allowed" in text:
        return "permission_error"
    if "input" in text or "validation" in text:
        return "input_error"
    return "unknown"


__all__ = ["AgentRuntime", "OpenHarnessRuntimeGateway"]
