from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from openharness.invest_research.report_sections import (
    ALL_SECTION_IDS,
    MODEL_SECTION_IDS,
    SectionContextBuilder,
    SectionReportOrchestrator,
    _validate_section_output,
)
from openharness.invest_research.runtime_adapter import AgentExecutionResult, UsageRecord


def _context() -> dict[str, Any]:
    facts = [
        {
            "fact_id": f"F-{index}",
            "statement": f"经营事实 {index}",
            "source_ids": [f"S-{index}"],
        }
        for index in range(1, 5)
    ]
    return {
        "run_id": "RUN-SECTION-TEST",
        "source_policy": "Only use authorized records.",
        "delivery": {"delivery_mode": "formal", "warnings": []},
        "company": {
            "legal_name": "科大讯飞股份有限公司",
            "short_name": "科大讯飞",
            "ticker": "002230.SZ",
            "source_ids": ["S-1"],
        },
        "research_period": {"start_date": "2025-08-13", "end_date": "2026-08-13"},
        "catalyst_window": {"start_date": "2026-08-14", "end_date": "2027-02-13"},
        "competitors": [
            {"company_name": "百度", "source_ids": ["S-2"]},
            {"company_name": "阿里巴巴", "source_ids": ["S-3"]},
        ],
        "operating_changes": facts,
        "investment_logics": [
            {
                "logic_id": f"L-{index}",
                "title": f"逻辑 {index}",
                "supporting_fact_ids": [f"F-{index}"],
                "source_ids": [f"S-{index}"],
                "tracking_indicators": [f"指标 {index}"],
                "falsification_conditions": [f"失效条件 {index}"],
            }
            for index in range(1, 4)
        ],
        "peer_comparison": [
            {"company_name": name, "source_ids": [f"S-{index}"]}
            for index, name in enumerate(("科大讯飞", "百度", "阿里巴巴"), start=1)
        ],
        "peer_comparison_limitations": [],
        "catalysts": [
            {
                "catalyst_id": "CAT-1",
                "title": "产品发布",
                "source_ids": ["S-4"],
                "affected_metrics": ["合同金额"],
            }
        ],
        "risks": [
            {
                "risk_id": "RISK-1",
                "title": "商业化不及预期",
                "source_ids": ["S-4"],
                "monitoring_plan": ["收入增速"],
            }
        ],
        "review": {
            "review_id": "REVIEW-SECTION-1",
            "decision": "approve_with_warnings",
            "limitations": ["部分口径待复核"],
        },
        "sources": [
            {"source_id": f"S-{index}", "title": f"来源 {index}"}
            for index in range(1, 5)
        ],
    }


class _Gateway:
    def __init__(self, fail_once: str | None = None) -> None:
        self.fail_once = fail_once
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def execute(self, **kwargs: Any) -> AgentExecutionResult:
        context = kwargs["context_package"]["report_section_context"]
        section_id = context["section_id"]
        self.calls.append(section_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if self.fail_once == section_id and self.calls.count(section_id) == 1:
            return AgentExecutionResult(
                status="failed",
                agent_id="report_writer",
                runtime_agent_name="investment-research:report_writer",
                failure_class="network",
                error="temporary network failure",
            )
        allowed = list(context["allowed_evidence_ids"])
        logic_ids = ["L-1", "L-2", "L-3"] if section_id == "investment_logics" else []
        evidence_ids = logic_ids or allowed[:1]
        peer_names = "科大讯飞、百度、阿里巴巴" if section_id == "peer_comparison" else ""
        output = {
            "protocol_version": "1.0",
            "status": "completed",
            "completed_scope": [section_id],
            "evidence_refs": evidence_ids,
            "unverified_items": [],
            "limitations": [],
            "handoff_requests": [],
            "blocking_reasons": [],
            "section_id": section_id,
            "title": f"章节 {section_id}",
            "content": f"本章结论与证据。{peer_names}",
            "evidence_ids": evidence_ids,
            "logic_ids": logic_ids,
            "warnings": [],
        }
        return AgentExecutionResult(
            status="succeeded",
            agent_id="report_writer",
            runtime_agent_name="investment-research:report_writer",
            model="deepseek-v4-flash",
            structured_output=output,
            usage=UsageRecord(input_tokens=100, output_tokens=50, total_tokens=150),
        )


def _input_base() -> dict[str, Any]:
    return {
        "protocol_version": "1.0",
        "run_id": "RUN-SECTION-TEST",
        "task_id": "TASK-REPORT-BASE-001",
        "objective": "分章节写作",
        "agent_id": "report_writer",
        "parameter_card": {"parameter_card_id": "PC-SECTION-1", "version": "1.0"},
        "review_id": "REVIEW-SECTION-1",
        "delivery_mode": "formal",
        "selected_logic_ids": ["L-1", "L-2", "L-3"],
        "approved_fact_ids": ["F-1", "F-2", "F-3"],
        "approved_logic_ids": ["L-1", "L-2", "L-3"],
        "approved_catalyst_ids": ["CAT-1"],
        "approved_risk_ids": ["RISK-1"],
    }


def test_section_context_is_bounded_and_section_specific() -> None:
    contexts = SectionContextBuilder(_context()).build_all()
    assert set(contexts) == set(MODEL_SECTION_IDS)
    assert "peer_comparison" not in contexts["operating_changes"]["section_material"]
    assert len(str(contexts["investment_logics"])) < 24_000


def test_unauthorized_evidence_is_rejected() -> None:
    context = SectionContextBuilder(_context()).build("company_overview")
    with pytest.raises(ValueError, match="unauthorized"):
        _validate_section_output(
            section_id="company_overview",
            payload={
                "status": "completed",
                "completed_scope": ["company_overview"],
                "evidence_refs": ["S-NOT-AUTHORIZED"],
                "unverified_items": [],
                "limitations": [],
                "handoff_requests": [],
                "blocking_reasons": [],
                "section_id": "company_overview",
                "title": "公司概况",
                "content": "内容",
                "evidence_ids": ["S-NOT-AUTHORIZED"],
                "logic_ids": [],
                "warnings": [],
            },
            context=context,
            selected_logic_ids=["L-1", "L-2", "L-3"],
        )


def _output_root() -> Path:
    path = Path(".openharness") / "test-output" / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_orchestrator_parallel_retry_and_assembly() -> None:
    output_root = _output_root()
    gateway = _Gateway(fail_once="operating_changes")
    outcome = asyncio.run(
        SectionReportOrchestrator(
            gateway=gateway,
            project_root=output_root,
            run_id="RUN-SECTION-TEST",
            report_context=_context(),
            input_base=_input_base(),
        ).run()
    )
    assert gateway.max_active == 3
    assert gateway.calls.count("operating_changes") == 2
    assert set(outcome.section_records) == set(ALL_SECTION_IDS)
    assert len(outcome.report_payload["sections"]) == 8
    assert outcome.report_payload["included_logic_ids"] == ["L-1", "L-2", "L-3"]
    assert not outcome.all_model_sections_failed


def test_completed_sections_are_reused() -> None:
    output_root = _output_root()
    first_gateway = _Gateway()
    asyncio.run(
        SectionReportOrchestrator(
            gateway=first_gateway,
            project_root=output_root,
            run_id="RUN-SECTION-TEST",
            report_context=_context(),
            input_base=_input_base(),
        ).run()
    )
    second_gateway = _Gateway()
    outcome = asyncio.run(
        SectionReportOrchestrator(
            gateway=second_gateway,
            project_root=output_root,
            run_id="RUN-SECTION-TEST",
            report_context=_context(),
            input_base=_input_base(),
        ).run()
    )
    assert second_gateway.calls == []
    assert all(outcome.section_records[key].reused for key in MODEL_SECTION_IDS)
    assert outcome.total_usage.total_tokens == 0
