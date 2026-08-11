from __future__ import annotations

import asyncio
from typing import Any

from openharness.invest_research.orchestration.research_flow import (
    InvestmentResearchSmokeFlow,
)
from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)
from openharness.invest_research.runtime_adapter import (
    AgentExecutionResult,
    UsageRecord,
)


class _FakeRuntime:
    def __init__(self, *, planner_success: bool = True) -> None:
        self.planner_success = planner_success
        self.requests: list[Any] = []

    async def execute_agent(self, request):
        self.requests.append(request)
        if request.agent_id == "planner":
            if not self.planner_success:
                return AgentExecutionResult(
                    status="failed",
                    agent_id="planner",
                    runtime_agent_name="investment-research:planner",
                    model="deepseek-v4-flash",
                    error="offline planner failure",
                )
            return AgentExecutionResult(
                status="succeeded",
                agent_id="planner",
                runtime_agent_name="investment-research:planner",
                model="deepseek-v4-flash",
                model_call_id="MODEL-PLANNER-001",
                structured_output={
                    "status": "completed",
                    "company_identity": {"short_name": "宁德时代"},
                },
                parameter_card_id="PC-CREWAI-001",
                artifact_id="ART-PLANNER-CREWAI-001",
                source_ids=["S-PLANNER-001"],
                usage=UsageRecord(input_tokens=100, output_tokens=20, total_tokens=120),
            )
        return AgentExecutionResult(
            status="succeeded",
            agent_id="fundamental",
            runtime_agent_name="investment-research:fundamental",
            model="deepseek-v4-flash",
            model_call_id="MODEL-FUNDAMENTAL-001",
            structured_output={"status": "partial"},
            artifact_id="ART-FUNDAMENTAL-CREWAI-001",
            source_ids=["S-FUNDAMENTAL-001"],
            fact_ids=["F-FUNDAMENTAL-001"],
            usage=UsageRecord(input_tokens=200, output_tokens=40, total_tokens=240),
        )


def _run_flow(runtime: _FakeRuntime):
    gateway = OpenHarnessRuntimeGateway(runtime)
    flow = InvestmentResearchSmokeFlow(gateway)
    result = asyncio.run(
        flow.kickoff_async(
            inputs={
                "run_id": "RUN-CREWAI-OFFLINE-001",
                "company_query": "宁德时代 CATL 300750.SZ",
                "as_of_date": "2026-08-11",
            }
        )
    )
    return flow, gateway, result


def test_flow_calls_openharness_once_per_agent_and_passes_parameter_card():
    runtime = _FakeRuntime()
    flow, gateway, result = _run_flow(runtime)

    assert result["pipeline_status"] == "completed"
    assert gateway.execution_count == 2
    assert [request.agent_id for request in runtime.requests] == ["planner", "fundamental"]
    fundamental_input = runtime.requests[1].input_payload
    assert fundamental_input["parameter_card"] == {
        "parameter_card_id": "PC-CREWAI-001",
        "version": "1.0",
    }
    assert runtime.requests[1].context_package["planner_artifact_id"] == ("ART-PLANNER-CREWAI-001")
    assert flow.state.orchestrator_llm_calls == 0
    assert result["runtime_executions"] == 2
    assert result["agent_results"]["planner"]["model_call_id"] == "MODEL-PLANNER-001"
    assert result["agent_results"]["fundamental"]["model_call_id"] == ("MODEL-FUNDAMENTAL-001")


def test_planner_failure_stops_before_fundamental():
    runtime = _FakeRuntime(planner_success=False)
    flow, gateway, result = _run_flow(runtime)

    assert result["pipeline_status"] == "planner_failed"
    assert gateway.execution_count == 1
    assert [request.agent_id for request in runtime.requests] == ["planner"]
    assert flow.state.current_stage == "failed"


def test_visible_events_show_real_handoff():
    runtime = _FakeRuntime()
    _, _, result = _run_flow(runtime)

    event_types = [event["event_type"] for event in result["events"]]
    assert event_types == [
        "task_started",
        "artifact_delivered",
        "task_handoff",
        "artifact_delivered",
    ]
    assert result["events"][2]["target_agent_ids"] == ["fundamental"]
    assert "@Fundamental" in result["events"][2]["message"]
