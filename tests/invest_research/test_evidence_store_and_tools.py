from __future__ import annotations

import asyncio
import json
import shutil
import unittest
from pathlib import Path
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
from openharness.invest_research.runtime_tools import BudgetedTool
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
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
        self.assertEqual(result.output, "ok")
        self.assertEqual(delegate.calls, 2)
        self.assertEqual(result.metadata["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
