from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    def __init__(
        self,
        *,
        planner_success: bool = True,
        failed_agent_ids: set[str] | None = None,
        report_writer_success: bool = True,
        risk_failure_class: str | None = None,
    ) -> None:
        self.planner_success = planner_success
        self.failed_agent_ids = failed_agent_ids or set()
        self.report_writer_success = report_writer_success
        self.risk_failure_class = risk_failure_class
        self.requests: list[Any] = []
        self.active_research = 0
        self.peak_active_research = 0
        self.evidence_store = _FakeEvidenceStore()

    async def execute_agent(self, request):
        self.requests.append(request)
        if request.agent_id == "report_writer":
            if not self.report_writer_success:
                return AgentExecutionResult(
                    status="failed",
                    agent_id="report_writer",
                    runtime_agent_name="investment-research:report_writer",
                    model="deepseek-v4-flash",
                    failure_class="network",
                    error="offline writer failure",
                )
            return AgentExecutionResult(
                status="succeeded",
                agent_id="report_writer",
                runtime_agent_name="investment-research:report_writer",
                model="deepseek-v4-flash",
                model_call_id="MODEL-WRITER-001",
                structured_output={
                    "status": "completed",
                    "completed_scope": ["report"],
                    "evidence_refs": [],
                    "unverified_items": [],
                    "limitations": [],
                    "handoff_requests": [],
                    "blocking_reasons": [],
                    "title": "宁德时代研究报告",
                    "metadata": {
                        "company_name": "宁德时代",
                        "ticker": "300750.SZ",
                        "as_of_date": "2026-08-11",
                        "research_period": {"start_date": "2025-08-12", "end_date": "2026-08-11"},
                        "catalyst_window": {"start_date": "2026-08-12", "end_date": "2027-02-11"},
                        "protocol_version": "1.0",
                    },
                    "sections": [
                        {
                            "section_id": section_id,
                            "title": section_id,
                            "content": "演示内容",
                            "evidence_ids": ["L-001"] if section_id not in {
                                "company_overview", "tracking_indicators", "limitations"
                            } else [],
                        }
                        for section_id in (
                            "company_overview",
                            "operating_changes",
                            "investment_logics",
                            "peer_comparison",
                            "catalysts",
                            "risks",
                            "tracking_indicators",
                            "limitations",
                        )
                    ],
                    "included_logic_ids": ["L-001", "L-002", "L-003"],
                    "compliance_statement": "本报告不构成投资建议。",
                },
                artifact_id="ART-REPORT-CREWAI-001",
                report_id="REPORT-CREWAI-001",
                usage=UsageRecord(input_tokens=100, output_tokens=30, total_tokens=130),
            )
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
                    "research_period": {
                        "start_date": "2025-08-12",
                        "end_date": "2026-08-11",
                    },
                    "catalyst_window": {
                        "start_date": "2026-08-12",
                        "end_date": "2027-02-11",
                    },
                    "recommended_competitors": [
                        {"company_name": "比亚迪", "ticker": "002594.SZ", "reason": "动力电池"},
                        {"company_name": "亿纬锂能", "ticker": "300014.SZ", "reason": "锂电池"},
                    ],
                },
                parameter_card_id="PC-CREWAI-001",
                artifact_id="ART-PLANNER-CREWAI-001",
                source_ids=["S-PLANNER-001"],
                usage=UsageRecord(input_tokens=100, output_tokens=20, total_tokens=120),
            )
        self.active_research += 1
        self.peak_active_research = max(
            self.peak_active_research, self.active_research
        )
        await asyncio.sleep(0.01)
        self.active_research -= 1
        agent_id = request.agent_id
        if agent_id == "risk" and self.risk_failure_class:
            return AgentExecutionResult(
                status="failed",
                agent_id="risk",
                runtime_agent_name="investment-research:risk",
                model="deepseek-v4-flash",
                model_call_id="MODEL-RISK-FAILED-001",
                failure_class=self.risk_failure_class,
                error="offline risk timeout",
            )
        if agent_id in self.failed_agent_ids:
            return AgentExecutionResult(
                status="invalid_output",
                agent_id=agent_id,
                runtime_agent_name=f"investment-research:{agent_id}",
                model="deepseek-v4-flash",
                model_call_id=f"MODEL-{agent_id.upper()}-INVALID-001",
                error=f"offline {agent_id} schema validation failure",
            )
        if agent_id == "reviewer_arbiter" and request.input_payload.get("task_id") == "TASK-CREWAI-FINAL-REVIEW-001":
            return AgentExecutionResult(
                status="succeeded",
                agent_id=agent_id,
                runtime_agent_name=f"investment-research:{agent_id}",
                model="deepseek-v4-flash",
                model_call_id="MODEL-FINAL-REVIEWER-001",
                structured_output={
                    "status": "completed",
                    "completed_scope": ["final review"],
                    "evidence_refs": [],
                    "unverified_items": [],
                    "limitations": [],
                    "handoff_requests": [],
                    "blocking_reasons": [],
                    "review_id": "REVIEW-CREWAI-FINAL-001",
                    "decision": "approve_with_warnings",
                    "approved_fact_ids": [],
                    "approved_logic_ids": ["L-001", "L-002", "L-003"],
                    "approved_catalyst_ids": [],
                    "approved_risk_ids": [],
                    "rejected_items": [],
                    "issues": [],
                    "conflicts": [],
                    "decision_rationale": "演示复审通过并保留警告。",
                },
                artifact_id="ART-FINAL-REVIEWER-CREWAI-001",
                review_id="REVIEW-CREWAI-FINAL-001",
                usage=UsageRecord(input_tokens=200, output_tokens=40, total_tokens=240),
            )
        if agent_id == "reviewer_arbiter":
            return AgentExecutionResult(
                status="succeeded",
                agent_id=agent_id,
                runtime_agent_name=f"investment-research:{agent_id}",
                model="deepseek-v4-flash",
                model_call_id="MODEL-REVIEWER-001",
                structured_output={
                    "status": "completed",
                    "decision": "request_supplement",
                    "issues": [
                        {
                            "issue_id": "ISSUE-CREWAI-001",
                            "target_agent_id": "fundamental",
                            "problem_statement": "补充财务事实来源。",
                        },
                        {
                            "issue_id": "ISSUE-CREWAI-002",
                            "target_agent_id": "risk",
                            "problem_statement": "复核风险触发条件。",
                        },
                    ],
                },
                artifact_id="ART-REVIEWER-ARBITER-CREWAI-001",
                review_id="REVIEW-CREWAI-001",
                usage=UsageRecord(input_tokens=200, output_tokens=40, total_tokens=240),
            )
        return AgentExecutionResult(
            status="succeeded",
            agent_id=agent_id,
            runtime_agent_name=f"investment-research:{agent_id}",
            model="deepseek-v4-flash",
            model_call_id=f"MODEL-{agent_id.upper()}-001",
            structured_output={
                "status": "partial",
                "completed_scope": [f"{agent_id} demo scope"],
                "limitations": ["demo limitation"],
                "unverified_items": [{"item": "demo item", "reason": "demo"}],
                "logic_candidates": [
                    {
                        "logic_id": f"L-{agent_id.upper()}-001",
                        "title": f"{agent_id} logic",
                        "mechanism": "demo",
                        "falsification_conditions": ["demo condition"],
                    }
                ],
                "events": [
                    {
                        "catalyst_id": f"CAT-{agent_id.upper()}-001",
                        "title": f"{agent_id} event",
                        "direction": "uncertain",
                        "failure_signals": ["demo failure signal"],
                    }
                ],
                "risk_items": [
                    {
                        "risk_id": f"R-{agent_id.upper()}-001",
                        "title": f"{agent_id} risk",
                        "trigger_conditions": ["demo trigger"],
                    }
                ],
            },
            artifact_id=f"ART-{agent_id.upper()}-CREWAI-001",
            source_ids=[f"S-{agent_id.upper()}-001"],
            fact_ids=[f"F-{agent_id.upper()}-001"],
            logic_ids=[f"L-{agent_id.upper()}-001"],
            catalyst_ids=[f"CAT-{agent_id.upper()}-001"],
            risk_ids=[f"R-{agent_id.upper()}-001"],
            review_id=(
                "REVIEW-CREWAI-001"
                if agent_id == "reviewer_arbiter"
                else None
            ),
            usage=UsageRecord(input_tokens=200, output_tokens=40, total_tokens=240),
        )


class _FakeEvidenceStore:
    def __init__(self):
        self.records: list[dict] = []

    def persist_agent_output(self, *, run_id, task_id, agent_id, payload):
        risk_ids = [
            item["risk_id"]
            for item in payload.get("risk_items", [])
            if isinstance(item, dict) and item.get("risk_id")
        ]
        self.records.append(
            {
                "record_type": "artifact",
                "record_id": f"ART-{agent_id.upper().replace('_', '-')}-FALLBACK-001",
                "run_id": run_id,
                "agent_id": agent_id,
                "submitted_by": agent_id,
                "status": payload.get("status", "partial"),
                "payload": payload,
            }
        )
        for risk_id in risk_ids:
            self.records.append(
                {
                    "record_type": "risk",
                    "record_id": risk_id,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "submitted_by": agent_id,
                    "status": "approved",
                    "payload": {"risk_id": risk_id},
                }
            )
        return {
            "artifact_id": f"ART-{agent_id.upper().replace('_', '-')}-FALLBACK-001",
            "risk_ids": risk_ids,
        }

    def run_exists(self, run_id):
        return bool(run_id)

    def get_record(self, run_id, record_id):
        for record in self.records:
            if record.get("run_id") == run_id and record.get("record_id") == record_id:
                return record
        if record_id.startswith("L-"):
            return {
                "record_type": "logic",
                "record_id": record_id,
                "run_id": run_id,
                "status": "approved",
                "payload": {
                    "logic_id": record_id,
                    "title": record_id,
                    "mechanism": "offline fixture",
                    "supporting_fact_ids": [],
                    "source_ids": [],
                },
            }
        return None

    def query_records(
        self,
        run_id,
        *,
        record_types=None,
        limit=100,
        status=None,
        statuses=(),
        submitted_by=(),
        ids=(),
        text_query=None,
    ):
        del status, statuses, ids, text_query
        selected = [
            record
            for record in self.records
            if record.get("run_id") == run_id
            and (not record_types or record.get("record_type") in record_types)
            and (not submitted_by or record.get("submitted_by") in submitted_by)
        ]
        if record_types and "parameter_card" in record_types and not selected:
            selected.append(
                {
                    "record_type": "parameter_card",
                    "record_id": "PC-CREWAI-001",
                    "run_id": run_id,
                    "status": "confirmed",
                    "payload": {
                        "parameter_card_id": "PC-CREWAI-001",
                        "company_identity": {
                            "short_name": "宁德时代",
                            "ticker": "300750.SZ",
                        },
                        "research_period": {
                            "start_date": "2025-08-12",
                            "end_date": "2026-08-11",
                        },
                        "catalyst_window": {
                            "start_date": "2026-08-12",
                            "end_date": "2027-02-11",
                        },
                        "recommended_competitors": [
                            {"company_name": "比亚迪"},
                            {"company_name": "LGES"},
                        ],
                    },
                }
            )
        return selected[:limit]

    def update_record_status(self, *args, **kwargs):
        return None


class _RetryRuntime:
    def __init__(self):
        self.calls = 0

    async def execute_agent(self, request):
        self.calls += 1
        if self.calls == 1:
            return AgentExecutionResult(
                status="failed",
                agent_id=request.agent_id,
                runtime_agent_name=f"investment-research:{request.agent_id}",
                failure_class="network",
                error="temporary network failure",
            )
        return AgentExecutionResult(
            status="succeeded",
            agent_id=request.agent_id,
            runtime_agent_name=f"investment-research:{request.agent_id}",
            model="deepseek-v4-flash",
        )


def _run_flow(runtime: _FakeRuntime, *, complete_report: bool = False):
    gateway = OpenHarnessRuntimeGateway(runtime)
    flow = InvestmentResearchSmokeFlow(gateway, complete_report=complete_report)
    output_root = (
        Path(__file__).resolve().parents[2]
        / ".openharness"
        / "data"
        / "tests"
        / f"flow-output-{uuid4().hex}"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    flow._project_root = output_root
    result = asyncio.run(
        flow.kickoff_async(
            inputs={
                "run_id": "RUN-CREWAI-OFFLINE-001",
                "company_query": "宁德时代 CATL 300750.SZ",
                "as_of_date": "2026-08-11",
            }
        )
    )
    try:
        return flow, gateway, result
    finally:
        shutil.rmtree(output_root, ignore_errors=True)


def test_flow_dispatches_parallel_research_risk_then_reviewer():
    runtime = _FakeRuntime()
    flow, gateway, result = _run_flow(runtime)

    assert result["pipeline_status"] == "awaiting_reviewer_recheck"
    assert gateway.execution_count == 8
    assert [request.agent_id for request in runtime.requests] == [
        "planner",
        "fundamental",
        "industry_competition",
        "market_catalyst",
        "risk",
        "reviewer_arbiter",
        "fundamental",
        "risk",
    ]
    assert runtime.peak_active_research == 3
    fundamental_input = next(
        request.input_payload
        for request in runtime.requests
        if request.agent_id == "fundamental"
    )
    assert fundamental_input["parameter_card"] == {
        "parameter_card_id": "PC-CREWAI-001",
        "version": "1.0",
    }
    assert all(
        request.context_package["planner_artifact_id"] == "ART-PLANNER-CREWAI-001"
        for request in runtime.requests[1:4]
    )
    assert flow.state.orchestrator_llm_calls == 0
    risk_request = next(
        request for request in runtime.requests if request.agent_id == "risk"
    )
    assert risk_request.input_payload["fundamental_artifact_id"] == (
        "ART-FUNDAMENTAL-CREWAI-001"
    )
    assert risk_request.input_payload["industry_competition_artifact_id"] == (
        "ART-INDUSTRY_COMPETITION-CREWAI-001"
    )
    assert risk_request.input_payload["market_catalyst_artifact_id"] == (
        "ART-MARKET_CATALYST-CREWAI-001"
    )
    assert risk_request.input_payload["logic_ids"]
    assert risk_request.max_turns == 3
    assert risk_request.tool_call_limits["evidence_query"] == 0
    assert "compact_research_brief" in risk_request.context_package
    reviewer_request = runtime.requests[5]
    assert set(reviewer_request.input_payload["research_artifact_ids"]) == {
        "ART-FUNDAMENTAL-CREWAI-001",
        "ART-INDUSTRY_COMPETITION-CREWAI-001",
        "ART-MARKET_CATALYST-CREWAI-001",
        "ART-RISK-CREWAI-001",
    }
    assert reviewer_request.max_turns == 3
    assert reviewer_request.tool_call_limits["evidence_query"] == 0
    assert "compact_research_brief" in reviewer_request.context_package
    supplement_request = runtime.requests[-2]
    assert supplement_request.agent_id == "fundamental"
    assert supplement_request.context_package["review_issues"][0]["issue_id"] == (
        "ISSUE-CREWAI-001"
    )
    risk_supplement_request = runtime.requests[-1]
    assert risk_supplement_request.agent_id == "risk"
    assert risk_supplement_request.input_payload["task_id"] == (
        "TASK-CREWAI-SUPPLEMENT-RISK-001"
    )
    assert result["runtime_executions"] == 8
    assert result["agent_results"]["planner"]["model_call_id"] == "MODEL-PLANNER-001"
    assert set(result["agent_results"]) == {
        "planner",
        "fundamental",
        "industry_competition",
        "market_catalyst",
        "risk",
        "reviewer_arbiter",
    }
    initial_starts = [
        event["created_at"]
        for event in result["events"]
        if event["event_type"] == "task_started" and event["actor_id"] == "planner"
    ]
    initial_completions = [
        event["created_at"]
        for event in result["events"]
        if event["event_type"] == "artifact_delivered"
        and event["target_agent_ids"] == ["planner"]
    ]
    assert max(initial_starts) < min(initial_completions)
    assert result["agent_results"]["risk"]["started_at"] >= max(initial_completions)
    assert result["agent_results"]["risk"]["output_status"] == "partial"
    initial_risk_delivery = next(
        event
        for event in result["events"]
        if event["actor_id"] == "risk" and event["target_agent_ids"] == ["system"]
    )
    initial_reviewer_handoff = next(
        event
        for event in result["events"]
        if event["event_type"] == "task_handoff"
        and event["target_agent_ids"] == ["reviewer_arbiter"]
    )
    assert initial_reviewer_handoff["created_at"] >= initial_risk_delivery["created_at"]
    assert result["agent_results"]["reviewer_arbiter"]["review_id"] == (
        "REVIEW-CREWAI-001"
    )


def test_planner_failure_stops_before_parallel_research():
    runtime = _FakeRuntime(planner_success=False)
    flow, gateway, result = _run_flow(runtime)

    assert result["pipeline_status"] == "planner_failed"
    assert gateway.execution_count == 1
    assert [request.agent_id for request in runtime.requests] == ["planner"]
    assert flow.state.current_stage == "failed"


def test_one_parallel_research_failure_continues_to_risk_and_reviewer():
    runtime = _FakeRuntime(failed_agent_ids={"industry_competition"})
    _, gateway, result = _run_flow(runtime)

    assert result["pipeline_status"] == "awaiting_reviewer_recheck"
    assert gateway.execution_count == 8
    assert result["agent_results"]["industry_competition"]["execution_status"] == (
        "invalid_output"
    )
    assert result["agent_results"]["risk"]["execution_status"] == "succeeded"
    assert result["agent_results"]["reviewer_arbiter"]["execution_status"] == (
        "succeeded"
    )
    risk_request = next(
        request for request in runtime.requests if request.agent_id == "risk"
    )
    assert risk_request.input_payload["industry_competition_artifact_id"] is None
    assert set(risk_request.context_package["missing_upstream_agents"]) == {
        "industry_competition"
    }
    reviewer_request = next(
        request
        for request in runtime.requests
        if request.agent_id == "reviewer_arbiter"
    )
    assert set(reviewer_request.context_package["missing_research_agents"]) == {
        "industry_competition"
    }
    assert any(
        event["event_type"] == "task_handoff"
        and event["target_agent_ids"] == ["risk"]
        and "部分并行研究" in event["message"]
        for event in result["events"]
    )


def test_complete_flow_uses_local_risk_artifact_when_risk_times_out():
    runtime = _FakeRuntime(risk_failure_class="timeout")
    _, _, result = _run_flow(runtime, complete_report=True)

    risk_result = result["agent_results"]["risk"]
    assert risk_result["execution_status"] == "succeeded"
    assert risk_result["fallback_used"] is True
    assert risk_result["artifact_id"]
    assert risk_result["risk_ids"]
    reviewer_request = next(
        request
        for request in runtime.requests
        if request.agent_id == "reviewer_arbiter"
        and request.input_payload["task_id"] == "TASK-CREWAI-REVIEWER-001"
    )
    assert reviewer_request.input_payload["research_artifact_ids"]
    assert risk_result["artifact_id"] in reviewer_request.input_payload["research_artifact_ids"]


def test_visible_events_show_real_handoff():
    runtime = _FakeRuntime()
    _, _, result = _run_flow(runtime)

    event_types = [event["event_type"] for event in result["events"]]
    assert event_types.count("task_started") == 5
    assert event_types.count("task_handoff") == 3
    assert event_types.count("artifact_delivered") == 8
    targets = {
        event["target_agent_ids"][0]
        for event in result["events"]
        if event["actor_id"] == "planner" and event["event_type"] == "task_started"
    }
    assert targets == {"fundamental", "industry_competition", "market_catalyst"}
    handoffs = [
        event for event in result["events"] if event["event_type"] == "task_handoff"
    ]
    risk_handoff = handoffs[0]
    assert risk_handoff["target_agent_ids"] == ["risk"]
    assert handoffs[1]["target_agent_ids"] == ["reviewer_arbiter"]


def test_complete_flow_generates_report_after_final_review():
    runtime = _FakeRuntime()
    flow, gateway, result = _run_flow(runtime, complete_report=True)

    assert result["report_generated"] is True
    assert result["report_quality"] in {"complete", "partial"}
    assert result["pipeline_status"] in {"completed", "completed_with_warnings"}
    assert result["report_path"].endswith("report.md")
    assert result["run_id"] in result["report_path"]
    assert [request.agent_id for request in runtime.requests][-2:] == [
        "reviewer_arbiter",
        "report_writer",
    ]
    writer_request = runtime.requests[-1]
    assert writer_request.max_turns == 2
    assert writer_request.tool_call_limits == {}
    assert "report_context" in writer_request.context_package
    assert writer_request.context_package["report_context"]["run_id"] == result["run_id"]
    assert flow.state.fallback_used is False
    assert gateway.execution_count == 10


def test_complete_flow_falls_back_when_report_writer_fails():
    runtime = _FakeRuntime(report_writer_success=False)
    gateway = OpenHarnessRuntimeGateway(runtime)
    flow = InvestmentResearchSmokeFlow(gateway, complete_report=True)
    temporary_root = (
        Path(__file__).resolve().parents[2]
        / ".openharness"
        / "data"
        / "tests"
        / f"flow-output-{uuid4().hex}"
    )
    temporary_root.mkdir(parents=True, exist_ok=True)
    flow._project_root = temporary_root
    result = asyncio.run(
        flow.kickoff_async(
            inputs={
                "run_id": "RUN-CREWAI-FALLBACK-001",
                "company_query": "宁德时代 CATL 300750.SZ",
                "as_of_date": "2026-08-11",
            }
        )
    )

    try:
        assert result["report_generated"] is True
        assert result["fallback_used"] is True
        assert result["report_quality"] == "fallback"
        assert result["pipeline_status"] == "completed_with_warnings"
        assert (flow._project_root / ".openharness" / "validation" / "full-chain-report.md").is_file()
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def test_gateway_retries_one_transient_network_failure():
    runtime = _RetryRuntime()
    gateway = OpenHarnessRuntimeGateway(runtime)
    result = asyncio.run(
        gateway.execute(
            agent_id="planner",
            input_payload={},
            task_prompt="test",
        )
    )

    assert result.status == "succeeded"
    assert runtime.calls == 2
    assert gateway.execution_count == 2
    assert result.retry_count == 1


def test_gateway_can_disable_transient_retry_for_bounded_flow_steps():
    runtime = _RetryRuntime()
    gateway = OpenHarnessRuntimeGateway(runtime)
    result = asyncio.run(
        gateway.execute(
            agent_id="risk",
            input_payload={},
            task_prompt="test",
            timeout_seconds=10,
            retry_transient=False,
        )
    )

    assert result.status == "failed"
    assert result.failure_class == "network"
    assert runtime.calls == 1
    assert gateway.execution_count == 1
