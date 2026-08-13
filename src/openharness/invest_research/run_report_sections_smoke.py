"""Generate and assemble report chapters for one existing reviewed Run."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from openharness.invest_research.delivery_decision import (
    DeliveryDecisionBuilder,
    register_recovery_review,
)
from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)
from openharness.invest_research.report_context import ReportContextBuilder
from openharness.invest_research.report_delivery import (
    write_report_context,
    write_report_markdown,
    write_run_summary,
)
from openharness.invest_research.report_sections import (
    MODEL_SECTION_IDS,
    SectionReportOrchestrator,
)
from openharness.invest_research.run_report_writer_smoke import (
    _latest_review,
    _parameter_card_id,
)
from openharness.invest_research.runtime_adapter import InvestmentResearchRuntimeAdapter


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


async def run(
    run_id: str,
    *,
    retry_partial: bool = False,
) -> tuple[dict[str, Any], Path]:
    root = _project_root()
    store = EvidenceStore.for_project(root)
    review = _latest_review(store, run_id)
    delivery = DeliveryDecisionBuilder(store).build(
        run_id,
        review,
        recovery_reason="report_sections_smoke_review_recovery" if not review else None,
    )
    if delivery.delivery_mode == "fallback":
        raise ValueError(f"Run {run_id} does not contain three traceable candidate logics")
    register_recovery_review(store, run_id, delivery, review)
    parameter_card_id = _parameter_card_id(store, run_id)
    delivery_payload = delivery.model_dump(mode="json")
    context = ReportContextBuilder(store, max_chars=24_000).build(
        run_id,
        review,
        delivery_decision=delivery_payload,
    )
    context_path = write_report_context(root, run_id, context)
    gateway = OpenHarnessRuntimeGateway(
        InvestmentResearchRuntimeAdapter(project_root=root, evidence_store=store)
    )

    def show_event(event_type: str, section_id: str, message: str) -> None:
        print(f"[{event_type}] {section_id}: {message}", flush=True)

    orchestrator = SectionReportOrchestrator(
        gateway=gateway,
        project_root=root,
        run_id=run_id,
        report_context=context,
        input_base={
            "protocol_version": "1.0",
            "run_id": run_id,
            "task_id": "TASK-REPORT-SECTIONS-SMOKE-001",
            "objective": "复用已有研究证据，分章节生成并组装研究报告。",
            "agent_id": "report_writer",
            "parameter_card": {
                "parameter_card_id": parameter_card_id,
                "version": "1.0",
            },
            "review_id": delivery.review_id,
            "delivery_mode": delivery.delivery_mode,
            "selected_logic_ids": list(delivery.selected_logic_ids),
            "approved_fact_ids": review.get("approved_fact_ids", []),
            "approved_logic_ids": (
                list(delivery.selected_logic_ids)
                if delivery.delivery_mode == "formal"
                else []
            ),
            "approved_catalyst_ids": review.get("approved_catalyst_ids", []),
            "approved_risk_ids": review.get("approved_risk_ids", []),
        },
        event_callback=show_event,
        reuse_partial=not retry_partial,
    )
    outcome = await orchestrator.run()
    persisted: dict[str, Any] = {}
    report_path: Path | None = None
    if not outcome.all_model_sections_failed:
        persisted = store.persist_agent_output(
            run_id=run_id,
            task_id="TASK-REPORT-SECTIONS-ASSEMBLE-001",
            agent_id="report_writer",
            payload=outcome.report_payload,
        )
        report_path = write_report_markdown(
            root,
            run_id,
            outcome.report_payload,
            review_output=review,
            delivery_decision=delivery_payload,
        )

    records = outcome.section_records
    statuses = {key: item.execution_status for key, item in records.items()}
    model_records = [records[section_id] for section_id in MODEL_SECTION_IDS]
    failed = [item.section_id for item in model_records if item.execution_status == "failed"]
    partial = [item.section_id for item in model_records if item.execution_status == "partial"]
    tool_count = 0
    payload = {
        "mode": "ReportSectionsSmoke",
        "run_id": run_id,
        "report_generated": report_path is not None,
        "formal_report_succeeded": report_path is not None,
        "report_quality": "partial" if failed or partial else "complete",
        "fallback_used": False,
        "delivery_mode": delivery.delivery_mode,
        "section_statuses": statuses,
        "completed_section_count": sum(value == "completed" for value in statuses.values()),
        "partial_section_ids": partial,
        "failed_section_ids": failed,
        "reused_section_ids": [
            key for key, item in records.items() if item.reused
        ],
        "section_retry_counts": {
            key: max(0, item.attempts - 1) for key, item in records.items()
        },
        "section_token_usage": {
            key: item.usage.total_tokens for key, item in records.items()
        },
        "section_durations": {
            key: round(item.duration_seconds, 3) for key, item in records.items()
        },
        "total_tokens": outcome.total_usage.total_tokens,
        "tool_count": tool_count,
        "artifact_id": persisted.get("artifact_id"),
        "report_id": persisted.get("report_id"),
        "context_path": str(context_path),
        "report_path": str(report_path) if report_path else None,
        "pass": bool(report_path is not None and not outcome.all_model_sections_failed),
    }
    summary_path = write_run_summary(
        root,
        run_id,
        payload,
        compatibility_name="report-sections-smoke.json",
    )
    return payload, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--retry-partial", action="store_true")
    args = parser.parse_args()
    payload, summary_path = asyncio.run(
        run(args.run_id, retry_partial=args.retry_partial)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Report sections smoke receipt: {summary_path}")
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
