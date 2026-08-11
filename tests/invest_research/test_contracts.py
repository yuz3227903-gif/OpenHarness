from __future__ import annotations

import unittest
from copy import deepcopy

from pydantic import ValidationError

from openharness.invest_research.contracts import INPUT_MODELS, OUTPUT_MODELS


BASE_INPUT = {
    "protocol_version": "1.0",
    "run_id": "RUN-DEMO-001",
    "task_id": "TASK-DEMO-001",
    "objective": "完成当前角色的结构化研究任务",
}

PARAMETER_CARD = {
    "parameter_card_id": "PC-DEMO-001",
    "version": "1.0",
}

DATE_RANGE = {
    "start_date": "2025-01-01",
    "end_date": "2025-12-31",
}

COMPETITORS = [
    {
        "company_name": "示例竞品甲",
        "ticker": "AAA",
        "selection_reasons": ["业务可比"],
    },
    {
        "company_name": "示例竞品乙",
        "ticker": "BBB",
        "selection_reasons": ["资料可得"],
    },
]


def valid_input_payloads() -> dict[str, dict]:
    return {
        "planner": {
            **BASE_INPUT,
            "agent_id": "planner",
            "company_query": "示例上市公司",
            "as_of_date": "2026-08-11",
        },
        "fundamental": {
            **BASE_INPUT,
            "agent_id": "fundamental",
            "parameter_card": PARAMETER_CARD,
        },
        "industry_competition": {
            **BASE_INPUT,
            "agent_id": "industry_competition",
            "parameter_card": PARAMETER_CARD,
            "confirmed_competitors": COMPETITORS,
        },
        "market_catalyst": {
            **BASE_INPUT,
            "agent_id": "market_catalyst",
            "parameter_card": PARAMETER_CARD,
            "catalyst_window": DATE_RANGE,
        },
        "risk": {
            **BASE_INPUT,
            "agent_id": "risk",
            "parameter_card": PARAMETER_CARD,
            "fundamental_artifact_id": "ART-FUND-001",
            "industry_competition_artifact_id": "ART-IND-001",
            "market_catalyst_artifact_id": "ART-CAT-001",
        },
        "reviewer_arbiter": {
            **BASE_INPUT,
            "agent_id": "reviewer_arbiter",
            "parameter_card": PARAMETER_CARD,
            "research_artifact_ids": [
                "ART-FUND-001",
                "ART-IND-001",
                "ART-CAT-001",
                "ART-RISK-001",
            ],
        },
        "report_writer": {
            **BASE_INPUT,
            "agent_id": "report_writer",
            "parameter_card": PARAMETER_CARD,
            "review_id": "REVIEW-DEMO-001",
            "approved_logic_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
        },
    }


def valid_output_payloads() -> dict[str, dict]:
    common = {
        "protocol_version": "1.0",
        "status": "partial",
        "completed_scope": [],
        "evidence_refs": [],
        "unverified_items": [],
        "limitations": ["结构化合同示例"],
        "handoff_requests": [],
        "blocking_reasons": [],
    }
    return {
        "planner": dict(common),
        "fundamental": dict(common),
        "industry_competition": dict(common),
        "market_catalyst": dict(common),
        "risk": dict(common),
        "reviewer_arbiter": {
            **common,
            "status": "completed",
            "review_id": "REVIEW-DEMO-001",
            "decision": "approve_for_report",
            "approved_logic_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
            "decision_rationale": "三条逻辑满足证据和审查要求",
        },
        "report_writer": dict(common),
    }


class ContractTests(unittest.TestCase):
    def test_every_output_inherits_the_common_handoff_fields(self):
        expected_fields = {
            "status",
            "completed_scope",
            "evidence_refs",
            "unverified_items",
            "limitations",
            "handoff_requests",
            "blocking_reasons",
        }

        for agent_id, model in OUTPUT_MODELS.items():
            with self.subTest(agent_id=agent_id):
                self.assertTrue(expected_fields.issubset(model.model_fields))

    def test_every_output_requires_each_common_handoff_field(self):
        common_fields = (
            "status",
            "completed_scope",
            "evidence_refs",
            "unverified_items",
            "limitations",
            "handoff_requests",
            "blocking_reasons",
        )

        for agent_id, model in OUTPUT_MODELS.items():
            for field_name in common_fields:
                with self.subTest(agent_id=agent_id, field_name=field_name):
                    payload = deepcopy(valid_output_payloads()[agent_id])
                    payload.pop(field_name)
                    with self.assertRaises(ValidationError):
                        model.model_validate(payload)

    def test_each_input_contract_accepts_a_valid_example(self):
        for agent_id, model in INPUT_MODELS.items():
            with self.subTest(agent_id=agent_id):
                result = model.model_validate(valid_input_payloads()[agent_id])
                self.assertEqual(result.agent_id, agent_id)

    def test_each_output_contract_accepts_a_valid_example(self):
        for agent_id, model in OUTPUT_MODELS.items():
            with self.subTest(agent_id=agent_id):
                result = model.model_validate(valid_output_payloads()[agent_id])
                self.assertIn(result.status, {"completed", "partial"})

    def test_input_contract_rejects_missing_required_field(self):
        for agent_id, model in INPUT_MODELS.items():
            with self.subTest(agent_id=agent_id):
                payload = deepcopy(valid_input_payloads()[agent_id])
                payload.pop("objective")
                with self.assertRaises(ValidationError):
                    model.model_validate(payload)

    def test_input_contract_rejects_another_agents_identity(self):
        for agent_id, model in INPUT_MODELS.items():
            with self.subTest(agent_id=agent_id):
                payload = deepcopy(valid_input_payloads()[agent_id])
                payload["agent_id"] = "planner" if agent_id != "planner" else "fundamental"
                with self.assertRaises(ValidationError):
                    model.model_validate(payload)

    def test_output_contract_rejects_invalid_status_enum(self):
        for agent_id, model in OUTPUT_MODELS.items():
            with self.subTest(agent_id=agent_id):
                payload = deepcopy(valid_output_payloads()[agent_id])
                payload["status"] = "looks_good"
                with self.assertRaises(ValidationError):
                    model.model_validate(payload)

    def test_blocked_result_must_explain_why_it_is_blocked(self):
        payload = valid_output_payloads()["fundamental"]
        payload["status"] = "blocked"
        payload["blocking_reasons"] = []

        with self.assertRaisesRegex(ValidationError, "blocking_reasons"):
            OUTPUT_MODELS["fundamental"].model_validate(payload)

    def test_review_approval_requires_exactly_three_logic_ids(self):
        payload = valid_output_payloads()["reviewer_arbiter"]
        payload["approved_logic_ids"] = ["L-DEMO-001", "L-DEMO-002"]

        with self.assertRaisesRegex(ValidationError, "exactly three"):
            OUTPUT_MODELS["reviewer_arbiter"].model_validate(payload)

    def test_review_approval_with_warnings_is_a_valid_delivery_route(self):
        payload = valid_output_payloads()["reviewer_arbiter"]
        payload["decision"] = "approve_with_warnings"

        result = OUTPUT_MODELS["reviewer_arbiter"].model_validate(payload)

        self.assertEqual(result.decision, "approve_with_warnings")

    def test_report_writer_input_requires_exactly_three_logic_ids(self):
        payload = valid_input_payloads()["report_writer"]
        payload["approved_logic_ids"] = ["L-DEMO-001", "L-DEMO-002"]

        with self.assertRaises(ValidationError):
            INPUT_MODELS["report_writer"].model_validate(payload)

    def test_risk_can_continue_with_one_available_upstream_artifact(self):
        payload = valid_input_payloads()["risk"]
        payload["industry_competition_artifact_id"] = None
        payload["market_catalyst_artifact_id"] = None

        result = INPUT_MODELS["risk"].model_validate(payload)

        self.assertIsNotNone(result.fundamental_artifact_id)

    def test_risk_rejects_when_every_upstream_artifact_is_missing(self):
        payload = valid_input_payloads()["risk"]
        payload["fundamental_artifact_id"] = None
        payload["industry_competition_artifact_id"] = None
        payload["market_catalyst_artifact_id"] = None

        with self.assertRaisesRegex(ValidationError, "at least one"):
            INPUT_MODELS["risk"].model_validate(payload)

    def test_contracts_reject_unknown_fields(self):
        payload = valid_input_payloads()["planner"]
        payload["override_governance"] = True

        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            INPUT_MODELS["planner"].model_validate(payload)


if __name__ == "__main__":
    unittest.main()
