"""Command-line entry point for the minimal CrewAI/OpenHarness flow."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from openharness.invest_research.orchestration import configure_crewai_environment


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _write_receipt(payload: dict[str, Any]) -> Path:
    output_dir = _project_root() / ".openharness" / "validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "crewai-flow-smoke.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def run(company: str, as_of_date: date) -> tuple[dict[str, Any], Path]:
    configure_crewai_environment(_project_root())
    from openharness.invest_research.orchestration.research_flow import (
        InvestmentResearchSmokeFlow,
    )

    flow = InvestmentResearchSmokeFlow()
    result = await flow.kickoff_async(
        inputs={
            "company_query": company,
            "as_of_date": as_of_date.isoformat(),
        }
    )
    payload = result if isinstance(result, dict) else flow.summary()
    payload["pass"] = (
        payload.get("pipeline_status") == "completed"
        and payload.get("runtime_executions") == 2
        and payload.get("orchestrator_llm_calls") == 0
    )
    path = _write_receipt(payload)
    return payload, path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", default="宁德时代 CATL 300750.SZ")
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        default=datetime.now(UTC).date(),
    )
    args = parser.parse_args()
    payload, path = asyncio.run(run(args.company, args.as_of_date))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"CrewAI flow receipt: {path}")
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
