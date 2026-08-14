from __future__ import annotations

from datetime import date

import pytest

from openharness.invest_research.agent_registry import get_agent_entry
from openharness.invest_research.workbench_agent_tasks import (
    DirectAgentTaskError,
    build_direct_agent_input,
    direct_task_budget,
)


RUN_ID = "RUN-WB-DIRECT-001"
PARAMETER_CARD_ID = "PC-WB-DIRECT-001"


class _EvidenceStore:
    def run_exists(self, run_id: str) -> bool:
        return run_id == RUN_ID

    def get_record(self, run_id: str, record_id: str):
        if run_id != RUN_ID or record_id != PARAMETER_CARD_ID:
            return None
        return {
            "record_type": "parameter_card",
            "record_id": PARAMETER_CARD_ID,
            "run_id": RUN_ID,
            "payload": {
                "parameter_card_id": PARAMETER_CARD_ID,
                "version": "1.0",
                "company_identity": {
                    "short_name": "科大讯飞",
                    "ticker": "002230.SZ",
                },
                "research_period": {
                    "start_date": "2025-08-14",
                    "end_date": "2026-08-14",
                },
                "catalyst_window": {
                    "start_date": "2026-08-15",
                    "end_date": "2027-02-14",
                },
                "recommended_competitors": [
                    {"company_name": "百度集团", "selection_reasons": ["大模型业务可比"]},
                    {"company_name": "拓维信息", "selection_reasons": ["教育与政企业务可比"]},
                ],
            },
        }


def _summary() -> dict:
    return {
        "run_id": RUN_ID,
        "parameter_card_id": PARAMETER_CARD_ID,
        "agent_results": {
            "fundamental": {
                "artifact_id": "ART-WB-FUND-001",
                "source_ids": ["S-WB-001"],
                "fact_ids": ["F-WB-001"],
                "logic_ids": ["L-WB-001"],
            },
            "industry_competition": {
                "artifact_id": "ART-WB-IND-001",
                "source_ids": ["S-WB-002"],
                "fact_ids": ["F-WB-002"],
                "logic_ids": ["L-WB-002"],
            },
            "market_catalyst": {
                "artifact_id": "ART-WB-CAT-001",
                "source_ids": ["S-WB-003"],
                "fact_ids": ["F-WB-003"],
                "logic_ids": ["L-WB-003"],
            },
            "risk": {"artifact_id": "ART-WB-RISK-001"},
        },
        "delivery_decision": {
            "delivery_mode": "provisional",
            "selected_logic_ids": ["L-WB-001", "L-WB-002", "L-WB-003"],
            "review_id": "REVIEW-WB-FINAL-001",
        },
    }


@pytest.mark.parametrize(
    "agent_id",
    [
        "fundamental",
        "industry_competition",
        "market_catalyst",
        "risk",
        "reviewer_arbiter",
        "report_writer",
    ],
)
def test_direct_agent_payload_matches_its_input_contract(agent_id: str) -> None:
    payload, context = build_direct_agent_input(
        agent_id=agent_id,
        objective="请补充当前研究并标记待验证信息",
        task_id=f"TASK-WB-{agent_id.upper().replace('_', '-')}-001",
        summary=_summary(),
        evidence_store=_EvidenceStore(),
        as_of_date=date(2026, 8, 14),
    )

    validated = get_agent_entry(agent_id).input_model.model_validate(payload)
    assert validated.agent_id == agent_id
    assert validated.run_id == RUN_ID
    assert context["channel_task"] is True
    assert direct_task_budget(agent_id)["max_turns"] >= 2


def test_industry_direct_task_keeps_exactly_two_confirmed_competitors() -> None:
    payload, _ = build_direct_agent_input(
        agent_id="industry_competition",
        objective="更新竞品比较",
        task_id="TASK-WB-INDUSTRY-001",
        summary=_summary(),
        evidence_store=_EvidenceStore(),
        as_of_date=date(2026, 8, 14),
    )

    assert [item["company_name"] for item in payload["confirmed_competitors"]] == [
        "百度集团",
        "拓维信息",
    ]


def test_report_writer_direct_task_uses_three_selected_logics() -> None:
    payload, _ = build_direct_agent_input(
        agent_id="report_writer",
        objective="根据现有材料重写报告摘要",
        task_id="TASK-WB-WRITER-001",
        summary=_summary(),
        evidence_store=_EvidenceStore(),
        as_of_date=date(2026, 8, 14),
    )

    assert payload["delivery_mode"] == "provisional"
    assert payload["selected_logic_ids"] == ["L-WB-001", "L-WB-002", "L-WB-003"]
    assert payload["approved_logic_ids"] == []


def test_direct_task_without_latest_run_is_blocked_clearly() -> None:
    with pytest.raises(DirectAgentTaskError, match="先 @Planner"):
        build_direct_agent_input(
            agent_id="fundamental",
            objective="补充基本面",
            task_id="TASK-WB-FUND-001",
            summary=None,
            evidence_store=_EvidenceStore(),
            as_of_date=date(2026, 8, 14),
        )


def test_risk_without_upstream_artifact_is_blocked() -> None:
    summary = _summary()
    summary["agent_results"] = {}
    with pytest.raises(DirectAgentTaskError, match="缺少上游研究成果"):
        build_direct_agent_input(
            agent_id="risk",
            objective="检查风险",
            task_id="TASK-WB-RISK-001",
            summary=summary,
            evidence_store=_EvidenceStore(),
            as_of_date=date(2026, 8, 14),
        )
