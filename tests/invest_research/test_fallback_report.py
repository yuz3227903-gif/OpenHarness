from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.fallback_report import write_fallback_report
from openharness.invest_research.runtime_adapter import AgentExecutionResult
from openharness.invest_research.validation_harness import ValidationHarness


class FallbackReportTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[2] / ".openharness" / "data" / "tests"
        self.root = root / uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(
            self.root / "evidence.sqlite3", upload_root=self.root / "uploads"
        )
        self.run_id = "RUN-FALLBACK-001"
        self.store.create_run(self.run_id, "宁德时代")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_fallback_always_writes_a_transparent_markdown_report(self) -> None:
        self.store.upsert_record(
            "logic",
            "L-FALLBACK-001",
            self.run_id,
            {
                "logic_id": "L-FALLBACK-001",
                "title": "候选逻辑",
                "mechanism": "仅用于测试已有记录渲染",
                "supporting_fact_ids": [],
            },
            status="candidate",
            submitted_by="fundamental",
        )
        output = self.root / "full-chain-report.md"
        result = write_fallback_report(
            store=self.store,
            run_id=self.run_id,
            output_path=output,
            results={
                "fundamental": AgentExecutionResult(
                    status="succeeded",
                    agent_id="fundamental",
                    runtime_agent_name="investment-research:fundamental",
                ),
                "risk": AgentExecutionResult(
                    status="failed",
                    agent_id="risk",
                    runtime_agent_name="investment-research:risk",
                    error="network error",
                ),
            },
            receipts=[],
            reason="risk_failed",
            review_output={
                "issues": [
                    {
                        "issue_id": "ISSUE-FALLBACK-001",
                        "problem_statement": "风险资料待补充",
                    }
                ]
            },
        )

        self.assertTrue(output.exists())
        markdown = output.read_text(encoding="utf-8")
        self.assertIn("三个最值得关注的投资逻辑", markdown)
        self.assertIn("风险资料待补充", markdown)
        self.assertIn("不构成投资建议", markdown)
        self.assertEqual(result["report_quality"], "partial")
        self.assertEqual(result["failed_agents"], ["risk"])

    def test_successful_writer_markdown_still_contains_required_headings(self) -> None:
        harness = ValidationHarness(self.root)
        result = AgentExecutionResult(
            status="succeeded",
            agent_id="report_writer",
            runtime_agent_name="investment-research:report_writer",
            report_id="REPORT-DEMO-001",
            structured_output={
                "title": "宁德时代研究报告",
                "sections": [
                    {
                        "section_id": "company",
                        "title": "公司概况",
                        "content": "示例内容",
                    }
                ],
                "compliance_statement": "不构成投资建议。",
            },
        )

        path = harness._write_agent_report_markdown(
            self.run_id, result, {"issues": []}
        )
        markdown = Path(path).read_text(encoding="utf-8")

        self.assertIn("## 公司概况", markdown)
        self.assertIn("## 最近一年经营变化", markdown)
        self.assertIn("## 三个最值得关注的投资逻辑", markdown)
        self.assertIn("## 两家主要竞争对手对比", markdown)
        self.assertIn("## 未来半年催化因素", markdown)
        self.assertIn("## 主要风险", markdown)


if __name__ == "__main__":
    unittest.main()
