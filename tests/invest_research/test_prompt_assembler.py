from __future__ import annotations

import unittest
from dataclasses import replace

from openharness.invest_research.prompt_assembler import (
    PromptAssembler,
    PromptAssemblyError,
    PromptLayers,
    assemble_prompt,
    render_output_contract,
)


class PromptAssemblerTests(unittest.TestCase):
    def setUp(self):
        self.complete_layers = PromptLayers(
            governance_prompt="GOVERNANCE-CONTENT",
            role_prompt="ROLE-CONTENT",
            task_prompt="TASK-CONTENT",
            context_package="CONTEXT-CONTENT",
            output_contract="CONTRACT-CONTENT",
        )

    def test_five_prompt_layers_are_assembled_in_fixed_order(self):
        prompt_text = PromptAssembler().assemble(self.complete_layers)

        positions = [
            prompt_text.index("GOVERNANCE-CONTENT"),
            prompt_text.index("ROLE-CONTENT"),
            prompt_text.index("TASK-CONTENT"),
            prompt_text.index("CONTEXT-CONTENT"),
            prompt_text.index("CONTRACT-CONTENT"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(prompt_text.count("<UNTRUSTED_RESEARCH_CONTENT>"), 2)
        self.assertEqual(prompt_text.count("</UNTRUSTED_RESEARCH_CONTENT>"), 2)

    def test_missing_any_prompt_layer_is_rejected(self):
        for missing_layer in (
            "governance_prompt",
            "role_prompt",
            "task_prompt",
            "context_package",
            "output_contract",
        ):
            with self.subTest(missing_layer=missing_layer):
                incomplete = replace(self.complete_layers, **{missing_layer: "   "})
                with self.assertRaisesRegex(PromptAssemblyError, missing_layer):
                    assemble_prompt(incomplete)

    def test_output_contract_is_rendered_from_pydantic_schema(self):
        rendered = render_output_contract("reviewer_arbiter")

        self.assertIn('"title": "ReviewDecision"', rendered)
        self.assertIn('"approved_logic_ids"', rendered)
        self.assertIn('"approve_for_report"', rendered)

    def test_unknown_agent_contract_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown investment-research agent_id"):
            render_output_contract("not-a-real-agent")


if __name__ == "__main__":
    unittest.main()
