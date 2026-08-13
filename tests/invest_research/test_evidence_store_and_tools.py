from __future__ import annotations

import asyncio
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pypdf import PdfWriter

from openharness.invest_research.calculator_tool import CalculatorInput, CalculatorTool
from openharness.invest_research.evidence_query_tool import (
    EvidenceQueryInput,
    EvidenceQueryTool,
)
from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.uploaded_file_tool import (
    ReadUploadedFileInput,
    ReadUploadedFileTool,
)
from openharness.invest_research.research_budget import DEFAULT_RESEARCH_BUDGET_POLICY
from openharness.invest_research.runtime_tools import (
    BudgetedTool,
    EvidenceAwareTavilyTool,
    EvidenceAwareWebFetchTool,
)
from openharness.invest_research.source_catalog import build_shared_source_catalog
from openharness.invest_research.tavily_tool import TavilySearchInput, TavilySearchTool
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.tools.web_fetch_tool import WebFetchTool, WebFetchToolInput
from pydantic import BaseModel


class _RetryInput(BaseModel):
    query: str


class _TransientReadOnlyTool(BaseTool):
    name = "transient_read_only"
    description = "Test-only read-only tool."
    input_model = _RetryInput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments, context):
        del arguments, context
        self.calls += 1
        if self.calls == 1:
            return ToolResult("temporary connection error", is_error=True)
        return ToolResult("ok")

    def is_read_only(self, arguments):
        del arguments
        return True


class EvidenceStoreAndToolTests(unittest.TestCase):
    def setUp(self) -> None:
        test_temp_root = Path(__file__).resolve().parents[2] / ".openharness" / "data" / "tests"
        self.root = test_temp_root / uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(
            self.root / "evidence.sqlite3",
            upload_root=self.root / "uploads",
        )
        self.store.create_run("RUN-ONE", "示例公司一")
        self.store.create_run("RUN-TWO", "示例公司二")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def context(self, run_id: str, **metadata) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=self.root,
            metadata={"run_id": run_id, **metadata},
        )

    def test_sources_and_queries_are_isolated_by_run(self):
        source_one = self.store.register_source(
            "RUN-ONE",
            url_or_file="https://example.com/report",
            title="Run one report",
        )
        source_two = self.store.register_source(
            "RUN-TWO",
            url_or_file="https://example.com/report",
            title="Run two report",
        )

        self.assertNotEqual(source_one, source_two)
        records = self.store.query_records("RUN-ONE", record_types=["source"])
        self.assertEqual([item["source_id"] for item in records], [source_one])
        self.assertFalse(self.store.record_exists("RUN-ONE", source_two))

    def test_evidence_query_enforces_authorized_and_approved_scopes(self):
        allowed = self.store.register_source(
            "RUN-ONE", url_or_file="https://example.com/a", title="A"
        )
        denied = self.store.register_source(
            "RUN-ONE", url_or_file="https://example.com/b", title="B"
        )
        tool = EvidenceQueryTool(self.store)

        denied_result = asyncio.run(
            tool.execute(
                EvidenceQueryInput(record_types=["source"], ids=[denied]),
                self.context(
                    "RUN-ONE",
                    evidence_scope="authorized_records",
                    authorized_refs={allowed},
                ),
            )
        )
        self.assertTrue(denied_result.is_error)
        self.assertIn("authorized", denied_result.output)

        allowed_result = asyncio.run(
            tool.execute(
                EvidenceQueryInput(record_types=["source"], ids=[allowed]),
                self.context(
                    "RUN-ONE",
                    evidence_scope="authorized_records",
                    authorized_refs={allowed},
                ),
            )
        )
        self.assertFalse(allowed_result.is_error)
        self.assertEqual(json.loads(allowed_result.output)["record_count"], 1)

        writer_denied = asyncio.run(
            tool.execute(
                EvidenceQueryInput(record_types=["source"], ids=[allowed]),
                self.context(
                    "RUN-ONE",
                    evidence_scope="approved_records_only",
                    approved_refs=set(),
                ),
            )
        )
        self.assertTrue(writer_denied.is_error)

    def test_calculator_allows_arithmetic_and_rejects_code(self):
        tool = CalculatorTool()
        success = asyncio.run(
            tool.execute(
                CalculatorInput(
                    expression="(revenue_now / revenue_prior - 1) * 100",
                    variables={"revenue_now": 120, "revenue_prior": 100},
                    precision=2,
                ),
                self.context("RUN-ONE"),
            )
        )
        self.assertFalse(success.is_error)
        self.assertEqual(json.loads(success.output)["result"], "20.00")

        dangerous = asyncio.run(
            tool.execute(
                CalculatorInput(expression="__import__('os').system('whoami')"),
                self.context("RUN-ONE"),
            )
        )
        self.assertTrue(dangerous.is_error)
        self.assertIn("not allowed", dangerous.output)

    def test_uploaded_text_and_pdf_require_a_registered_current_run_ref(self):
        run_upload_dir = self.store.upload_root / "RUN-ONE"
        run_upload_dir.mkdir(parents=True)
        text_path = run_upload_dir / "report.md"
        text_path.write_text("公开资料内容", encoding="utf-8")
        text_ref = self.store.register_uploaded_file("RUN-ONE", text_path)

        pdf_path = run_upload_dir / "report.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        with pdf_path.open("wb") as stream:
            writer.write(stream)
        pdf_ref = self.store.register_uploaded_file("RUN-ONE", pdf_path)

        tool = ReadUploadedFileTool(self.store)
        text_result = asyncio.run(
            tool.execute(
                ReadUploadedFileInput(file_ref=text_ref, max_chars=500),
                self.context("RUN-ONE", agent_id="fundamental", authorized_refs=set()),
            )
        )
        self.assertFalse(text_result.is_error)
        self.assertIn("公开资料内容", text_result.output)
        self.assertTrue(text_result.metadata["source_id"].startswith("S-"))

        pdf_result = asyncio.run(
            tool.execute(
                ReadUploadedFileInput(file_ref=pdf_ref, max_chars=500),
                self.context("RUN-ONE", agent_id="fundamental", authorized_refs=set()),
            )
        )
        self.assertFalse(pdf_result.is_error)
        self.assertIn("pages 1-1", pdf_result.output)

        cross_run = asyncio.run(
            tool.execute(
                ReadUploadedFileInput(file_ref=text_ref),
                self.context("RUN-TWO", agent_id="fundamental", authorized_refs=set()),
            )
        )
        self.assertTrue(cross_run.is_error)
        self.assertIn("not registered", cross_run.output)

    def test_uploaded_registration_rejects_paths_outside_run_directory(self):
        outside = self.root / "outside.txt"
        outside.write_text("do not read", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inside the current Run"):
            self.store.register_uploaded_file("RUN-ONE", outside)

    def test_budgeted_read_only_tool_retries_one_transient_failure(self):
        delegate = _TransientReadOnlyTool()
        tool = BudgetedTool(delegate)
        result = asyncio.run(
            tool.execute(
                _RetryInput(query="test"),
                self.context(
                    "RUN-ONE",
                    tool_limits={"transient_read_only": 2},
                    tool_counts={},
                ),
            )
        )

        self.assertFalse(result.is_error)
        self.assertTrue(result.output.startswith("ok"))
        self.assertIn("budget is now exhausted", result.output)
        self.assertEqual(delegate.calls, 2)
        self.assertEqual(result.metadata["retry_count"], 1)

    def test_budget_exhaustion_is_a_non_error_stop_signal(self):
        delegate = _TransientReadOnlyTool()
        delegate.calls = 1
        tool = BudgetedTool(delegate)
        context = self.context(
            "RUN-ONE",
            tool_limits={"transient_read_only": 1},
            tool_counts={"transient_read_only": 1},
        )

        result = asyncio.run(
            tool.execute(_RetryInput(query="do not run"), context)
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.metadata["reason"], "tool_budget_exhausted")
        self.assertEqual(delegate.calls, 1)
        self.assertIn("Do not call this tool again", result.output)

    def test_tavily_cache_is_shared_inside_one_run_and_isolated_between_runs(self):
        calls: list[TavilySearchInput] = []

        async def fake_search(_tool, arguments, context):
            del context
            calls.append(arguments)
            return ToolResult(
                json.dumps(
                    {
                        "provider": "tavily",
                        "query": arguments.query,
                        "result_count": 1,
                        "results": [
                            {
                                "title": "Official filing",
                                "url": "https://www.cninfo.com.cn/report",
                                "content": "x" * 1_200,
                                "score": 0.9,
                                "published_date": "2026-08-01",
                            }
                        ],
                    }
                )
            )

        async def run_twice():
            tool = EvidenceAwareTavilyTool(self.store)
            context = self.context("RUN-ONE", agent_id="planner", authorized_refs=set())
            first = await tool.execute(
                TavilySearchInput(query="  CATL   filing  ", max_results=8), context
            )
            second = await tool.execute(
                TavilySearchInput(query="catl filing", max_results=8), context
            )
            cross_run = await tool.execute(
                TavilySearchInput(query="catl filing", max_results=8),
                self.context("RUN-TWO", agent_id="planner", authorized_refs=set()),
            )
            return first, second, cross_run, context

        with patch.object(TavilySearchTool, "execute", new=fake_search):
            first, second, cross_run, context = asyncio.run(run_twice())

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].max_results, 3)
        self.assertFalse(first.metadata["cache_hit"])
        self.assertTrue(second.metadata["cache_hit"])
        self.assertFalse(cross_run.metadata["cache_hit"])
        first_payload = json.loads(first.output)
        self.assertLessEqual(
            len(first_payload["results"][0]["content"]),
            DEFAULT_RESEARCH_BUDGET_POLICY.tavily_snippet_chars,
        )
        self.assertIn(first_payload["source_ids"][0], context.metadata["authorized_refs"])

    def test_web_fetch_cache_normalizes_fragment_and_caps_page_length(self):
        calls: list[WebFetchToolInput] = []

        async def fake_fetch(_tool, arguments, context):
            del context
            calls.append(arguments)
            return ToolResult("page:" + "y" * arguments.max_chars)

        async def run_twice():
            tool = EvidenceAwareWebFetchTool(self.store)
            context = self.context("RUN-ONE", agent_id="fundamental", authorized_refs=set())
            first = await tool.execute(
                WebFetchToolInput(
                    url="HTTPS://Example.com/report#section-one", max_chars=50_000
                ),
                context,
            )
            second = await tool.execute(
                WebFetchToolInput(
                    url="https://example.com/report#section-two", max_chars=50_000
                ),
                context,
            )
            return first, second

        with patch.object(WebFetchTool, "execute", new=fake_fetch):
            first, second = asyncio.run(run_twice())

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].max_chars,
            DEFAULT_RESEARCH_BUDGET_POLICY.web_fetch_max_chars,
        )
        self.assertFalse(first.metadata["cache_hit"])
        self.assertTrue(second.metadata["cache_hit"])
        self.assertEqual(first.metadata["source_id"], second.metadata["source_id"])

    def test_failed_tool_results_are_not_cached(self):
        calls = 0

        async def failed_search(_tool, arguments, context):
            nonlocal calls
            del arguments, context
            calls += 1
            return ToolResult("temporary network failure", is_error=True)

        async def run_twice():
            tool = EvidenceAwareTavilyTool(self.store)
            context = self.context("RUN-ONE", agent_id="planner", authorized_refs=set())
            first = await tool.execute(TavilySearchInput(query="test query"), context)
            second = await tool.execute(TavilySearchInput(query="test query"), context)
            return first, second

        with patch.object(TavilySearchTool, "execute", new=failed_search):
            first, second = asyncio.run(run_twice())

        self.assertTrue(first.is_error)
        self.assertTrue(second.is_error)
        self.assertEqual(calls, 2)

    def test_source_catalog_prioritizes_official_sources(self):
        low = self.store.register_source(
            "RUN-ONE",
            url_or_file="https://example.com/commentary",
            title="Commentary",
            source_grade="C",
        )
        official = self.store.register_source(
            "RUN-ONE",
            url_or_file="https://www.cninfo.com.cn/filing",
            title="CATL annual report",
            source_grade="A",
            status="fetched",
        )
        catalog = build_shared_source_catalog(
            self.store, "RUN-ONE", entities=["CATL"]
        )

        self.assertEqual(catalog[0]["source_id"], official)
        self.assertEqual(catalog[0]["related_entity"], "CATL")
        self.assertEqual(catalog[-1]["source_id"], low)

    def test_partial_comparison_and_catalyst_outputs_recover_traceable_candidates(self):
        source = self.store.register_source(
            "RUN-ONE",
            url_or_file="https://www.cninfo.com.cn/example",
            title="Official filing",
            source_grade="A",
        )
        industry = self.store.persist_agent_output(
            run_id="RUN-ONE",
            task_id="TASK-INDUSTRY",
            agent_id="industry_competition",
            payload={
                "status": "partial",
                "completed_scope": ["comparison row"],
                "evidence_refs": [source],
                "unverified_items": [],
                "limitations": [],
                "handoff_requests": [],
                "blocking_reasons": [],
                "peer_comparison": [
                    {
                        "company_name": "Target",
                        "values": {"revenue": 100, "margin": "20%"},
                        "source_ids": [source],
                    }
                ],
                "relative_strengths": ["业务规模具有可比优势"],
                "relative_weaknesses": [],
                "logic_candidates": [],
            },
        )
        market = self.store.persist_agent_output(
            run_id="RUN-ONE",
            task_id="TASK-MARKET",
            agent_id="market_catalyst",
            payload={
                "status": "partial",
                "completed_scope": ["catalyst events"],
                "evidence_refs": [source],
                "unverified_items": [],
                "limitations": [],
                "handoff_requests": [],
                "blocking_reasons": [],
                "events": [
                    {
                        "catalyst_id": f"CAT-{index}",
                        "title": f"Event {index}",
                        "event_type": "announcement",
                        "evidence_class": "public_report",
                        "confidence_basis": "source-backed",
                        "transmission_path": "business expectation",
                        "direction": "positive",
                        "source_ids": [source],
                        "failure_signals": [],
                        "affected_metrics": ["revenue"],
                        "related_fact_ids": [],
                    }
                    for index in range(1, 4)
                ],
                "logic_candidates": [],
            },
        )

        self.assertEqual(len(industry["fact_ids"]), 1)
        self.assertEqual(len(industry["logic_ids"]), 1)
        self.assertEqual(len(market["fact_ids"]), 3)
        self.assertEqual(len(market["logic_ids"]), 3)
        for logic_id in [*industry["logic_ids"], *market["logic_ids"]]:
            logic = self.store.get_record("RUN-ONE", logic_id)
            self.assertEqual(logic["record_type"], "logic")
            fact_id = logic["payload"]["supporting_fact_ids"][0]
            fact = self.store.get_record("RUN-ONE", fact_id)
            self.assertEqual(fact["record_type"], "fact")
            self.assertIn(source, fact["payload"]["source_ids"])


if __name__ == "__main__":
    unittest.main()
