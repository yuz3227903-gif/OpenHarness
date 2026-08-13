from __future__ import annotations

import json
import unittest
from pathlib import Path

from openharness.invest_research.agent_registry import PLUGIN_NAME, iter_agent_entries
from openharness.invest_research.export_schemas import export_contract_schemas
from openharness.invest_research.contracts import (
    ReportSectionRequest,
    ReportSectionResult,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / ".openharness" / "plugins" / PLUGIN_NAME / "schemas"
PLUGIN_ROOT = SCHEMA_ROOT.parent


class SchemaExportTests(unittest.TestCase):
    def test_exporter_writes_agent_and_report_section_schemas_from_pydantic(self):
        written = export_contract_schemas(PLUGIN_ROOT)

        self.assertEqual(len(written), 16)
        self.assertTrue(all(path.exists() for path in written))

    def test_committed_schemas_match_the_canonical_pydantic_models(self):
        expected_names: set[str] = set()

        for entry in iter_agent_entries():
            pairs = (
                ("input", entry.input_model.model_json_schema()),
                ("output", entry.output_model.model_json_schema()),
            )
            for kind, expected_schema in pairs:
                with self.subTest(agent_id=entry.agent_id, kind=kind):
                    filename = f"{entry.agent_id}.{kind}.schema.json"
                    expected_names.add(filename)
                    actual_schema = json.loads(
                        (SCHEMA_ROOT / filename).read_text(encoding="utf-8")
                    )
                    self.assertEqual(actual_schema, expected_schema)

        for filename, expected_schema in (
            ("report_section.input.schema.json", ReportSectionRequest.model_json_schema()),
            ("report_section.output.schema.json", ReportSectionResult.model_json_schema()),
        ):
            expected_names.add(filename)
            actual_schema = json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
            self.assertEqual(actual_schema, expected_schema)

        self.assertEqual(
            {path.name for path in SCHEMA_ROOT.glob("*.schema.json")},
            expected_names,
        )


if __name__ == "__main__":
    unittest.main()
