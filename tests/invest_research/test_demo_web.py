from __future__ import annotations

import unittest
from pathlib import Path

from openharness.invest_research.demo_web import (
    WEB_ROOT,
    _compact_result,
    _latest_result,
    _remember_result,
)
from openharness.invest_research.runtime_adapter import (
    AgentExecutionResult,
    ToolCallTrace,
    UsageRecord,
)
from openharness.tools.web_fetch_tool import _is_readable_content_type


class PlannerDemoWebTests(unittest.TestCase):
    def test_static_page_assets_exist_and_are_valid_utf8(self):
        for name in ("index.html", "styles.css", "app.js"):
            path = WEB_ROOT / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\ufffd", text)
            self.assertGreater(len(text), 100)

    def test_compact_result_excludes_raw_model_and_full_tool_output(self):
        result = AgentExecutionResult(
            status="succeeded",
            agent_id="planner",
            runtime_agent_name="investment-research:planner",
            model="deepseek-v4-flash",
            structured_output={
                "status": "complete",
                "recommended_competitors": [],
                "task_plan": [],
            },
            raw_output="full model output must not reach the page",
            tool_calls=[
                ToolCallTrace(
                    tool_name="tavily_search",
                    tool_input={"query": "sample"},
                    output="x" * 1000,
                    is_error=False,
                )
            ],
            usage=UsageRecord(input_tokens=10, output_tokens=5, total_tokens=15),
        )

        compact = _compact_result("RUN-WEB-TEST", result)

        self.assertNotIn("raw_output", compact)
        self.assertLessEqual(len(compact["tool_calls"][0]["output_preview"]), 261)
        self.assertEqual(compact["usage"]["total_tokens"], 15)

    def test_web_fetch_accepts_text_and_rejects_pdf(self):
        self.assertTrue(_is_readable_content_type("text/html; charset=utf-8"))
        self.assertTrue(_is_readable_content_type("application/json"))
        self.assertFalse(_is_readable_content_type("application/pdf"))

    def test_latest_result_is_copied_for_safe_page_recovery(self):
        original = {"run_id": "RUN-WEB-CACHE", "nested": {"status": "succeeded"}}
        _remember_result(original)

        restored = _latest_result()

        self.assertEqual(restored, original)
        self.assertIsNot(restored, original)


if __name__ == "__main__":
    unittest.main()
