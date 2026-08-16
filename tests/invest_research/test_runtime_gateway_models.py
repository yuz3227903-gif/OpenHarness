from __future__ import annotations

import asyncio

from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)
from openharness.invest_research.runtime_adapter import AgentExecutionResult


def test_runtime_gateway_resolves_model_for_each_agent():
    class RecordingRuntime:
        def __init__(self) -> None:
            self.requests = []

        async def execute_agent(self, request):
            self.requests.append(request)
            return AgentExecutionResult(
                status="succeeded",
                agent_id=request.agent_id,
                runtime_agent_name=f"investment-research:{request.agent_id}",
                model=request.model_override,
            )

    runtime = RecordingRuntime()
    gateway = OpenHarnessRuntimeGateway(
        runtime,
        model_resolver=lambda agent_id: {
            "planner": "glm-5.3",
            "risk": "deepseek-v4-pro",
        }.get(agent_id),
    )

    result = asyncio.run(
        gateway.execute(
            agent_id="planner",
            input_payload={"company": "CATL"},
            task_prompt="Plan the research task.",
            retry_transient=False,
        )
    )

    assert runtime.requests[0].model_override == "glm-5.3"
    assert result.model == "glm-5.3"
