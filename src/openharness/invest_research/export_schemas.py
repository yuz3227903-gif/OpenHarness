"""Generate inspectable JSON Schemas from the canonical Pydantic contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openharness.invest_research.agent_registry import iter_agent_entries


def default_plugin_root() -> Path:
    return Path(__file__).resolve().parents[3] / ".openharness" / "plugins" / "investment-research"


def export_contract_schemas(plugin_root: Path | None = None) -> list[Path]:
    """Write deterministic input/output schemas and return their paths."""

    root = (plugin_root or default_plugin_root()).resolve()
    schema_dir = root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for entry in iter_agent_entries():
        pairs = (
            ("input", entry.input_model.model_json_schema()),
            ("output", entry.output_model.model_json_schema()),
        )
        for contract_kind, schema in pairs:
            path = schema_dir / f"{entry.agent_id}.{contract_kind}.schema.json"
            path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, default=None)
    args = parser.parse_args()
    for path in export_contract_schemas(args.plugin_root):
        print(path)


if __name__ == "__main__":
    main()
