from __future__ import annotations

import unittest

from openharness.invest_research.research_budget import ResearchBudgetPolicy


class ResearchBudgetPolicyTests(unittest.TestCase):
    def test_balanced_profile_has_finite_role_specific_limits(self):
        policy = ResearchBudgetPolicy()

        planner = policy.for_agent("planner")
        market = policy.for_agent("market_catalyst")
        reviewer = policy.for_agent("reviewer_arbiter", stage="final")
        writer = policy.for_agent("report_writer")

        self.assertEqual(planner.max_turns, 6)
        self.assertEqual(planner.tool_call_limits["tavily_search"], 4)
        self.assertEqual(market.tool_call_limits["tavily_search"], 3)
        self.assertEqual(reviewer.tool_call_limits, {"evidence_query": 1})
        self.assertEqual(writer.tool_call_limits, {})
        self.assertEqual(writer.max_output_tokens, 12_000)

    def test_returned_tool_limits_are_not_shared_mutable_state(self):
        policy = ResearchBudgetPolicy()
        first = policy.for_agent("fundamental")
        first.tool_call_limits["tavily_search"] = 99

        second = policy.for_agent("fundamental")
        self.assertEqual(second.tool_call_limits["tavily_search"], 2)


if __name__ == "__main__":
    unittest.main()
