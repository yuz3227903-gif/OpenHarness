from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from openharness.config.settings import Settings
from openharness.invest_research.agent_registry import (
    AGENT_REGISTRY,
    PLUGIN_NAME,
    iter_agent_entries,
    resolve_runtime_agent_name,
)
from openharness.plugins import load_plugins


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = PROJECT_ROOT / ".openharness" / "plugins" / PLUGIN_NAME

EXPECTED_IDS = (
    "planner",
    "fundamental",
    "industry_competition",
    "market_catalyst",
    "risk",
    "reviewer_arbiter",
    "report_writer",
)

EXPECTED_MAX_TURNS = {
    "planner": 8,
    "fundamental": 16,
    "industry_competition": 16,
    "market_catalyst": 12,
    "risk": 12,
    "reviewer_arbiter": 10,
    "report_writer": 2,
}

EXPECTED_TOOLS = {
    "planner": ("tavily_search", "web_fetch"),
    "fundamental": (
        "tavily_search",
        "web_fetch",
        "read_uploaded_file",
        "calculator",
        "evidence_query",
    ),
    "industry_competition": (
        "tavily_search",
        "web_fetch",
        "read_uploaded_file",
        "calculator",
        "evidence_query",
    ),
    "market_catalyst": (
        "tavily_search",
        "web_fetch",
        "read_uploaded_file",
        "calculator",
        "evidence_query",
    ),
    "risk": (
        "evidence_query",
        "tavily_search",
        "web_fetch",
        "read_uploaded_file",
        "calculator",
    ),
    "reviewer_arbiter": ("evidence_query",),
    "report_writer": (),
}


class PluginAndRegistryTests(unittest.TestCase):
    def load_investment_plugin(self):
        isolated_user_plugins = PROJECT_ROOT / ".nonexistent-user-plugins"
        with patch(
            "openharness.plugins.loader.get_user_plugins_dir",
            return_value=isolated_user_plugins,
        ):
            plugins = load_plugins(Settings(allow_project_plugins=True), PROJECT_ROOT)
        matches = [plugin for plugin in plugins if plugin.manifest.name == PLUGIN_NAME]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_project_plugin_is_not_enabled_without_explicit_trust(self):
        isolated_user_plugins = PROJECT_ROOT / ".nonexistent-user-plugins"
        with patch(
            "openharness.plugins.loader.get_user_plugins_dir",
            return_value=isolated_user_plugins,
        ):
            plugins = load_plugins(Settings(), PROJECT_ROOT)
        self.assertTrue(all(plugin.manifest.name != PLUGIN_NAME for plugin in plugins))

    def test_plugin_loads_exactly_seven_agents(self):
        plugin = self.load_investment_plugin()
        loaded = {agent.name: agent for agent in plugin.agents}

        self.assertEqual(len(loaded), 7)
        self.assertEqual([tool.name for tool in plugin.tools], ["tavily_search"])
        self.assertEqual(set(loaded), {f"{PLUGIN_NAME}:{agent_id}" for agent_id in EXPECTED_IDS})

        for entry in iter_agent_entries():
            with self.subTest(agent_id=entry.agent_id):
                agent = loaded[entry.runtime_agent_name]
                self.assertEqual(agent.model, "inherit")
                self.assertEqual(agent.max_turns, entry.max_turns)
                self.assertEqual(tuple(agent.tools or ()), entry.allowed_tools)
                self.assertEqual(
                    set(agent.disallowed_tools or ()),
                    set(entry.disallowed_tools),
                )
                self.assertTrue(agent.system_prompt)

    def test_registry_is_the_unique_stable_identity_map(self):
        entries = iter_agent_entries()

        self.assertEqual(tuple(AGENT_REGISTRY), EXPECTED_IDS)
        self.assertEqual(len({entry.agent_id for entry in entries}), 7)
        self.assertEqual(len({entry.runtime_agent_name for entry in entries}), 7)
        self.assertEqual(len({entry.output_contract_name for entry in entries}), 7)

        for entry in entries:
            with self.subTest(agent_id=entry.agent_id):
                self.assertEqual(
                    resolve_runtime_agent_name(entry.agent_id),
                    f"{PLUGIN_NAME}:{entry.agent_id}",
                )
                self.assertEqual(entry.model, "inherit")
                self.assertEqual(entry.max_turns, EXPECTED_MAX_TURNS[entry.agent_id])
                self.assertEqual(entry.allowed_tools, EXPECTED_TOOLS[entry.agent_id])
                self.assertEqual(entry.runtime_status, "validation_ready")
                self.assertEqual(entry.pending_tools, ())
                self.assertNotIn("pending_implementation", entry.tool_statuses.values())

    def test_reviewer_has_read_only_access_and_writer_has_no_tools(self):
        reviewer = AGENT_REGISTRY["reviewer_arbiter"]
        writer = AGENT_REGISTRY["report_writer"]

        self.assertEqual(reviewer.allowed_tools, ("evidence_query",))
        self.assertEqual(reviewer.evidence_query_scope, "run_records_read_only")
        self.assertEqual(writer.allowed_tools, ())
        self.assertEqual(writer.evidence_query_scope, "none")
        self.assertIn("evidence_query", writer.disallowed_tools)

        forbidden_external_tools = {
            "tavily_search",
            "web_fetch",
            "read_uploaded_file",
            "calculator",
        }
        self.assertTrue(forbidden_external_tools.isdisjoint(reviewer.allowed_tools))
        self.assertTrue(forbidden_external_tools.isdisjoint(writer.allowed_tools))

    def test_all_agents_forbid_delegation_shell_and_writes(self):
        minimum_forbidden = {
            "agent",
            "task_create",
            "send_message",
            "team_create",
            "team_delete",
            "bash",
            "write_file",
            "edit_file",
            "notebook_edit",
            "config",
        }

        for entry in iter_agent_entries():
            with self.subTest(agent_id=entry.agent_id):
                self.assertTrue(minimum_forbidden.issubset(entry.disallowed_tools))

    def test_role_prompts_are_professional_and_company_agnostic(self):
        plugin = self.load_investment_plugin()
        loaded = {agent.name: agent for agent in plugin.agents}
        forbidden_company_facts = ("宁德时代", "比亚迪", "CATL", "LGES", "300750")
        required_role_markers = {
            "planner": ("Gate 1", "竞品", "任务"),
            "fundamental": ("最近一年", "经营质量", "F-ID"),
            "industry_competition": ("两家竞品", "not_comparable", "比较口径"),
            "market_catalyst": ("未来六个月", "传导路径", "推测"),
            "risk": ("反方", "证伪", "L-ID"),
            "reviewer_arbiter": ("只读", "request_supplement", "恰好三条"),
            "report_writer": ("已批准", "八段式", "不搜索"),
        }

        for agent_id, markers in required_role_markers.items():
            with self.subTest(agent_id=agent_id):
                prompt_text = loaded[f"{PLUGIN_NAME}:{agent_id}"].system_prompt or ""
                self.assertTrue(all(marker in prompt_text for marker in markers))
                self.assertTrue(
                    all(company_fact not in prompt_text for company_fact in forbidden_company_facts)
                )

        governance = (PLUGIN_ROOT / "prompts" / "governance.md").read_text(encoding="utf-8")
        self.assertIn("运行时权限合同", governance)
        self.assertIn("禁止编造", governance)
        self.assertIn("不可信研究内容", governance)


if __name__ == "__main__":
    unittest.main()
