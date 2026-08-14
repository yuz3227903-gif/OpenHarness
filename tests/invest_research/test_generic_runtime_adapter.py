from __future__ import annotations

import asyncio
import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import Settings
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.stream_events import ErrorEvent
from openharness.invest_research.agent_registry import AGENT_REGISTRY
from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    ToolCallTrace,
    InvestmentResearchRuntimeAdapter,
    _classify_failure,
)


RUN_ID = "RUN-GENERIC-TEST-001"
PARAMETER_CARD = {"parameter_card_id": "PC-DEMO-001", "version": "1.0"}
COMPETITORS = [
    {"company_name": "竞品甲", "selection_reasons": ["业务可比"]},
    {"company_name": "竞品乙", "selection_reasons": ["资料可得"]},
]


class _StaticApiClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.closed = False

    async def stream_message(self, request):
        self.last_request = request
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=self.text)],
            ),
            usage=UsageSnapshot(input_tokens=12, output_tokens=8),
            stop_reason=None,
        )

    async def close(self) -> None:
        self.closed = True


class _SequentialApiClient(_StaticApiClient):
    """Return one invalid result first, then a schema-only repair result."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(texts[0])
        self.texts = texts
        self.call_count = 0

    async def stream_message(self, request):
        self.last_request = request
        text = self.texts[min(self.call_count, len(self.texts) - 1)]
        self.call_count += 1
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=text)],
            ),
            usage=UsageSnapshot(input_tokens=12, output_tokens=8),
            stop_reason=None,
        )


class _EmptyThenJsonApiClient:
    """Simulate a transient empty provider response followed by valid JSON."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.call_count = 0
        self.closed = False

    async def stream_message(self, request):
        self.call_count += 1
        if self.call_count == 1:
            yield ErrorEvent(
                message=(
                    "Model returned an empty assistant message. "
                    "The turn was ignored to keep the session healthy."
                )
            )
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=self.text)],
            ),
            usage=UsageSnapshot(input_tokens=12, output_tokens=8),
            stop_reason=None,
        )

    async def close(self) -> None:
        self.closed = True


class _EmptyCompleteThenJsonApiClient(_EmptyThenJsonApiClient):
    """Simulate a stream that completes with an empty assistant text."""

    async def stream_message(self, request):
        self.call_count += 1
        if self.call_count == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[TextBlock(text="")],
                ),
                usage=UsageSnapshot(input_tokens=12, output_tokens=0),
                stop_reason="stop",
            )
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=self.text)],
            ),
            usage=UsageSnapshot(input_tokens=12, output_tokens=8),
            stop_reason=None,
        )


def _common_input(agent_id: str) -> dict:
    return {
        "protocol_version": "1.0",
        "run_id": RUN_ID,
        "task_id": f"TASK-{agent_id.upper().replace('_', '-')}-001",
        "objective": "离线验证通用运行路由",
        "agent_id": agent_id,
    }


def _inputs() -> dict[str, dict]:
    return {
        "planner": {
            **_common_input("planner"),
            "company_query": "示例公司",
            "as_of_date": "2026-08-11",
        },
        "fundamental": {
            **_common_input("fundamental"),
            "parameter_card": PARAMETER_CARD,
        },
        "industry_competition": {
            **_common_input("industry_competition"),
            "parameter_card": PARAMETER_CARD,
            "confirmed_competitors": COMPETITORS,
        },
        "market_catalyst": {
            **_common_input("market_catalyst"),
            "parameter_card": PARAMETER_CARD,
            "catalyst_window": {
                "start_date": "2026-08-12",
                "end_date": "2027-02-11",
            },
        },
        "risk": {
            **_common_input("risk"),
            "parameter_card": PARAMETER_CARD,
            "fundamental_artifact_id": "ART-FUND-001",
            "industry_competition_artifact_id": "ART-IND-001",
            "market_catalyst_artifact_id": "ART-CAT-001",
        },
        "reviewer_arbiter": {
            **_common_input("reviewer_arbiter"),
            "parameter_card": PARAMETER_CARD,
            "research_artifact_ids": [
                "ART-FUND-001",
                "ART-IND-001",
                "ART-CAT-001",
                "ART-RISK-001",
            ],
        },
        "report_writer": {
            **_common_input("report_writer"),
            "parameter_card": PARAMETER_CARD,
            "review_id": "REVIEW-DEMO-001",
            "approved_logic_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
        },
    }


def _output(agent_id: str) -> str:
    payload = {
        "protocol_version": "1.0",
        "status": "partial",
        "completed_scope": ["离线运行路由"],
        "evidence_refs": [],
        "unverified_items": [],
        "limitations": ["离线测试未调用真实工具"],
        "handoff_requests": [],
        "blocking_reasons": [],
    }
    if agent_id == "reviewer_arbiter":
        payload.update(
            {
                "status": "completed",
                "review_id": "REVIEW-DEMO-002",
                "decision": "approve_for_report",
                "approved_logic_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
                "decision_rationale": "固定测试证据已满足离线合同",
            }
        )
    return json.dumps(payload, ensure_ascii=False)


class GenericRuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(__file__).resolve().parents[2] / ".openharness" / "data" / "tests"
        self.root = base / uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(
            self.root / "evidence.sqlite3",
            upload_root=self.root / "uploads",
        )
        self.store.create_run(RUN_ID, "示例公司")
        self.store.upsert_record(
            "parameter_card",
            "PC-DEMO-001",
            RUN_ID,
            PARAMETER_CARD,
            status="confirmed",
            submitted_by="planner",
        )
        for artifact_id in ("ART-FUND-001", "ART-IND-001", "ART-CAT-001", "ART-RISK-001"):
            self.store.upsert_record(
                "artifact",
                artifact_id,
                RUN_ID,
                {"artifact_id": artifact_id},
                status="completed",
                submitted_by="fixture",
            )
        for logic_id in ("L-DEMO-001", "L-DEMO-002", "L-DEMO-003"):
            self.store.upsert_record(
                "logic",
                logic_id,
                RUN_ID,
                {"logic_id": logic_id},
                status="approved",
                submitted_by="fixture",
            )
        self.store.upsert_record(
            "review",
            "REVIEW-DEMO-001",
            RUN_ID,
            {"review_id": "REVIEW-DEMO-001", "decision": "approve_for_report"},
            status="approve_for_report",
            submitted_by="reviewer_arbiter",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def adapter(self, text: str) -> InvestmentResearchRuntimeAdapter:
        return InvestmentResearchRuntimeAdapter(
            evidence_store=self.store,
            settings_loader=lambda: Settings(),
            api_client_factory=lambda settings: _StaticApiClient(text),
            require_search_configuration=False,
        )

    def test_each_role_gets_exactly_its_registry_whitelist(self):
        adapter = self.adapter(_output("planner"))
        for agent_id, entry in AGENT_REGISTRY.items():
            with self.subTest(agent_id=agent_id):
                tools = adapter.build_restricted_tool_registry(agent_id).list_tools()
                self.assertEqual(tuple(item.name for item in tools), entry.allowed_tools)
                self.assertTrue(set(entry.disallowed_tools).isdisjoint(item.name for item in tools))

    def test_all_seven_roles_can_execute_through_generic_router(self):
        for agent_id, input_payload in _inputs().items():
            with self.subTest(agent_id=agent_id):
                result = asyncio.run(
                    self.adapter(_output(agent_id)).execute_agent(
                        AgentExecutionRequest(
                            agent_id=agent_id,
                            input_payload=input_payload,
                        )
                    )
                )
                self.assertEqual(result.status, "succeeded")
                self.assertEqual(result.usage.total_tokens, 20)
                self.assertEqual(result.agent_id, agent_id)
                self.assertIsNotNone(result.artifact_id)

    def test_report_writer_can_use_section_contract_without_persisting_each_chapter(self):
        section_output = {
            "protocol_version": "1.0",
            "status": "completed",
            "completed_scope": ["investment_logics"],
            "evidence_refs": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
            "unverified_items": [],
            "limitations": [],
            "handoff_requests": [],
            "blocking_reasons": [],
            "section_id": "investment_logics",
            "title": "三个最值得关注的投资逻辑",
            "content": "基于三条已授权候选逻辑形成章节。",
            "evidence_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
            "logic_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
            "warnings": [],
        }
        payload = {
            **_inputs()["report_writer"],
            "section_id": "investment_logics",
            "allowed_evidence_ids": ["L-DEMO-001", "L-DEMO-002", "L-DEMO-003"],
            "attempt": 1,
        }
        result = asyncio.run(
            self.adapter(json.dumps(section_output, ensure_ascii=False)).execute_agent(
                AgentExecutionRequest(
                    agent_id="report_writer",
                    input_payload=payload,
                    output_contract="report_section",
                    persist_output=False,
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.structured_output["section_id"], "investment_logics")
        self.assertIsNone(result.artifact_id)
        self.assertIsNone(result.report_id)

    def test_reviewer_uses_backend_issued_review_and_audit_metadata(self):
        audit_id = "ART-EVIDENCE-AUDIT-DEMO-FINAL"
        self.store.upsert_record(
            "artifact",
            audit_id,
            RUN_ID,
            {"artifact_id": audit_id, "artifact_type": "evidence_audit"},
            status="pass_with_warnings",
            submitted_by="system",
        )
        input_payload = _inputs()["reviewer_arbiter"]
        input_payload.update(
            {
                "review_stage": "final",
                "expected_review_id": "REVIEW-DEMO-FINAL-001",
                "audit_artifact_id": audit_id,
                "audit_status": "pass_with_warnings",
            }
        )

        result = asyncio.run(
            self.adapter(_output("reviewer_arbiter")).execute_agent(
                AgentExecutionRequest(
                    agent_id="reviewer_arbiter",
                    input_payload=input_payload,
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.review_id, "REVIEW-DEMO-FINAL-001")
        self.assertEqual(result.structured_output["review_stage"], "final")
        self.assertEqual(result.structured_output["audit_artifact_id"], audit_id)
        self.assertEqual(result.structured_output["audit_status"], "pass_with_warnings")

    def test_unknown_cross_run_input_reference_blocks_before_model(self):
        payload = _inputs()["fundamental"]
        payload["source_ids"] = ["S-DOES-NOT-EXIST"]
        result = asyncio.run(
            self.adapter(_output("fundamental")).execute_agent(
                AgentExecutionRequest(agent_id="fundamental", input_payload=payload)
            )
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("not registered", result.error or "")

    def test_invalid_completed_industry_output_is_repaired_to_partial(self):
        invalid_completed = {
            "protocol_version": "1.0",
            "status": "completed",
            "completed_scope": ["竞品比较"],
            "evidence_refs": [],
            "unverified_items": [],
            "limitations": [],
            "handoff_requests": [],
            "blocking_reasons": [],
            "industry_definition": "动力电池行业",
            "comparison_dictionary": [
                {
                    "metric_name": "市场份额",
                    "formula_or_definition": "装机量占比",
                    "period": "2026H1",
                }
            ],
            "peer_comparison": [
                {"company_name": "宁德时代", "values": {"市场份额": "待验证"}},
                {"company_name": "竞品甲", "values": {"市场份额": "待验证"}},
                {"company_name": "竞品乙", "values": {"市场份额": "待验证"}},
            ],
            "logic_candidates": [],
        }
        repaired_partial = json.loads(_output("industry_competition"))
        repaired_partial["unverified_items"] = [
            {
                "item": "竞争差异对应的可核验候选逻辑",
                "reason": "现有来源不足",
                "required_evidence": "三家公司同口径经营数据",
            }
        ]
        client = _SequentialApiClient(
            [
                json.dumps(invalid_completed, ensure_ascii=False),
                json.dumps(repaired_partial, ensure_ascii=False),
            ]
        )
        adapter = InvestmentResearchRuntimeAdapter(
            evidence_store=self.store,
            settings_loader=lambda: Settings(),
            api_client_factory=lambda settings: client,
            require_search_configuration=False,
        )

        result = asyncio.run(
            adapter.execute_agent(
                AgentExecutionRequest(
                    agent_id="industry_competition",
                    input_payload=_inputs()["industry_competition"],
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.structured_output["status"], "partial")
        self.assertEqual(client.call_count, 2)
        self.assertTrue(any("repair attempt 1" in warning for warning in result.warnings))

    def test_empty_model_response_is_retried_once(self):
        client = _EmptyThenJsonApiClient(_output("planner"))
        adapter = InvestmentResearchRuntimeAdapter(
            evidence_store=self.store,
            settings_loader=lambda: Settings(),
            api_client_factory=lambda settings: client,
            require_search_configuration=False,
        )

        result = asyncio.run(
            adapter.execute_agent(
                AgentExecutionRequest(
                    agent_id="planner",
                    input_payload=_inputs()["planner"],
                )
            )
        )

        self.assertEqual(
            result.status,
            "succeeded",
            f"error={result.error}; warnings={result.warnings}; calls={client.call_count}",
        )
        self.assertEqual(client.call_count, 2)
        self.assertTrue(any("empty model response" in warning for warning in result.warnings))
        self.assertTrue(client.closed)

    def test_empty_completed_assistant_turn_is_retried_once(self):
        client = _EmptyCompleteThenJsonApiClient(_output("planner"))
        adapter = InvestmentResearchRuntimeAdapter(
            evidence_store=self.store,
            settings_loader=lambda: Settings(),
            api_client_factory=lambda settings: client,
            require_search_configuration=False,
        )

        result = asyncio.run(
            adapter.execute_agent(
                AgentExecutionRequest(
                    agent_id="planner",
                    input_payload=_inputs()["planner"],
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(client.call_count, 2)
        self.assertTrue(any("empty model response" in warning for warning in result.warnings))

    def test_execution_exceeded_is_classified_as_timeout_before_tool_error(self):
        failure_class = _classify_failure(
            "Risk execution exceeded 120 seconds.",
            [ToolCallTrace(tool_name="evidence_query", tool_input={}, is_error=True)],
        )

        self.assertEqual(failure_class, "timeout")


if __name__ == "__main__":
    unittest.main()
