"""Command-line entry point for the parallel CrewAI/OpenHarness smoke flow."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from openharness.invest_research.orchestration import configure_crewai_environment
from openharness.invest_research.report_delivery import write_run_summary


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _write_receipt(payload: dict[str, Any], *, complete_report: bool) -> Path:
    compatibility_name = (
        "full-chain.json" if complete_report else "crewai-flow-smoke.json"
    )
    return write_run_summary(
        _project_root(),
        str(payload["run_id"]),
        payload,
        compatibility_name=compatibility_name,
    )


async def run(
    company: str,
    as_of_date: date,
    *,
    complete_report: bool = False,
    pause_callback: Any | None = None,
    event_callback: Any | None = None,
    gateway: Any | None = None,
) -> tuple[dict[str, Any], Path]:
    configure_crewai_environment(_project_root())
    from openharness.invest_research.orchestration.research_flow import (
        InvestmentResearchSmokeFlow,
    )

    flow = InvestmentResearchSmokeFlow(
        gateway,
        complete_report=complete_report,
        pause_callback=pause_callback,
        event_callback=event_callback,
    )
    result = await flow.kickoff_async(
        inputs={
            "company_query": company,
            "as_of_date": as_of_date.isoformat(),
        }
    )
    payload = result if isinstance(result, dict) else flow.summary()
    base_pass = (
        payload.get("pipeline_status") in {"completed", "awaiting_reviewer_recheck", "completed_with_warnings"}
        and payload.get("runtime_executions", 0) >= 6
        and {
            "planner",
            "fundamental",
            "industry_competition",
            "market_catalyst",
            "risk",
            "reviewer_arbiter",
        }.issubset(set(payload.get("agent_results", {})))
        and payload["agent_results"]["risk"].get("output_status")
        in {"completed", "partial"}
        and payload["agent_results"]["reviewer_arbiter"].get("output_status")
        in {"completed", "partial"}
        and payload["agent_results"]["reviewer_arbiter"].get("review_id")
        and payload.get("orchestrator_llm_calls") == 0
    )
    payload["pass"] = bool(payload.get("report_generated")) if complete_report else base_pass
    path = _write_receipt(payload, complete_report=complete_report)
    return payload, path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", default="宁德时代 CATL 300750.SZ")
    parser.add_argument("--complete-report", action="store_true")
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        default=datetime.now(UTC).date(),
    )
    args = parser.parse_args()
    payload, path = asyncio.run(
        run(args.company, args.as_of_date, complete_report=args.complete_report)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"CrewAI flow receipt: {path}")
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
