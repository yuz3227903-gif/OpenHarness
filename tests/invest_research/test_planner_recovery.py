from __future__ import annotations

from datetime import date

from openharness.invest_research.planner_recovery import (
    is_competitor_placeholder,
    recover_partial_planner_output,
)


def _partial_output() -> dict:
    return {
        "protocol_version": "1.0",
        "status": "partial",
        "completed_scope": ["company identity", "date windows"],
        "evidence_refs": ["S-PLANNER-001"],
        "unverified_items": [],
        "limitations": ["second competitor was not confirmed"],
        "handoff_requests": [],
        "blocking_reasons": [],
        "company_identity": {
            "legal_name": "科大讯飞股份有限公司",
            "short_name": "科大讯飞",
            "ticker": "002230",
            "exchange": "深圳证券交易所",
            "primary_business": "人工智能",
            "source_ids": ["S-PLANNER-001"],
        },
        "research_period": {
            "start_date": "2025-08-13",
            "end_date": "2026-08-13",
        },
        "catalyst_window": {
            "start_date": "2026-08-13",
            "end_date": "2027-02-13",
        },
        "competitor_candidates": [],
        "recommended_competitors": [
            {
                "company_name": "百度集团",
                "ticker": "9888.HK",
                "exchange": "香港交易所",
                "selection_reasons": ["大模型业务可比"],
                "comparability_limits": ["上市市场不同"],
                "source_ids": ["S-PLANNER-001"],
            }
        ],
        "task_plan": [],
        "dependency_graph": {},
        "available_materials": [],
        "material_gaps": [],
        "gate_1_payload": None,
    }


def test_recovery_builds_provisional_card_and_explicit_competitor_placeholder():
    outcome = recover_partial_planner_output(
        run_id="RUN-PLANNER-RECOVERY-001",
        company_query="科大讯飞",
        as_of_date=date(2026, 8, 13),
        output=_partial_output(),
        source_ids=["S-PLANNER-001"],
    )

    assert outcome is not None
    assert outcome.placeholder_competitor_count == 1
    assert len(outcome.payload["recommended_competitors"]) == 2
    assert is_competitor_placeholder(outcome.payload["recommended_competitors"][1])
    assert outcome.payload["status"] == "partial"
    assert outcome.payload["gate_1_payload"] is not None
    assert len(outcome.payload["task_plan"]) == 6
    assert {
        item["assigned_agent_id"] for item in outcome.payload["task_plan"]
    } == {
        "fundamental",
        "industry_competition",
        "market_catalyst",
        "risk",
        "reviewer_arbiter",
        "report_writer",
    }
    assert any(
        item["target_agent_id"] == "industry_competition"
        for item in outcome.payload["handoff_requests"]
    )


def test_recovery_allows_structured_output_without_sources_but_marks_unverified():
    payload = _partial_output()
    payload["company_identity"]["source_ids"] = []
    payload["evidence_refs"] = []

    outcome = recover_partial_planner_output(
        run_id="RUN-PLANNER-RECOVERY-002",
        company_query="科大讯飞",
        as_of_date=date(2026, 8, 13),
        output=payload,
        source_ids=[],
    )

    assert outcome is not None
    assert outcome.payload["evidence_refs"] == []
    assert any(
        "没有可持久化的 S-ID" in item for item in outcome.payload["limitations"]
    )
    assert any(
        item["item"] == "Planner 原始参数的来源链"
        for item in outcome.payload["unverified_items"]
    )
    assert outcome.reason.startswith(
        "planner_partial_parameter_card_recovered_without_traceable_sources"
    )


def test_recovery_uses_two_real_candidates_without_placeholder():
    payload = _partial_output()
    payload["competitor_candidates"] = [
        {
            "company_name": "云从科技",
            "ticker": "688327.SH",
            "exchange": "上海证券交易所",
            "selection_reasons": ["人工智能业务可比"],
            "comparability_limits": [],
            "source_ids": ["S-PLANNER-002"],
        }
    ]
    outcome = recover_partial_planner_output(
        run_id="RUN-PLANNER-RECOVERY-003",
        company_query="科大讯飞",
        as_of_date=date(2026, 8, 13),
        output=payload,
        source_ids=["S-PLANNER-001", "S-PLANNER-002"],
    )

    assert outcome is not None
    assert outcome.placeholder_competitor_count == 0
    assert all(
        not is_competitor_placeholder(item)
        for item in outcome.payload["recommended_competitors"]
    )
