"""Run ReportWriter against one existing reviewed research Run."""

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
from openharness.invest_research.fallback_report import write_fallback_report
from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)
from openharness.invest_research.report_context import ReportContextBuilder
from openharness.invest_research.report_delivery import (
    run_output_dir,
    write_report_context,
    write_report_markdown,
    write_run_summary,
)
from openharness.invest_research.runtime_adapter import InvestmentResearchRuntimeAdapter


_REPORT_CONTEXT_MAX_CHARS = 24_000


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _latest_review(store: EvidenceStore, run_id: str) -> dict[str, Any]:
    records = store.query_records(run_id, record_types=("review",), limit=10)
    for record in records:
        payload = dict(record.get("payload") or {})
        if payload.get("decision") in {"approve_for_report", "approve_with_warnings"}:
            return payload
    return {}


def _parameter_card_id(store: EvidenceStore, run_id: str) -> str:
    records = store.query_records(run_id, record_types=("parameter_card",), limit=1)
    if not records:
        raise ValueError(f"Run {run_id} has no ParameterCard")
    payload = records[0].get("payload") or {}
    value = payload.get("parameter_card_id")
    if not value:
        raise ValueError(f"Run {run_id} ParameterCard has no ID")
    return str(value)


async def run(run_id: str) -> tuple[dict[str, Any], Path]:
    root = _project_root()
    store = EvidenceStore.for_project(root)
    review = _latest_review(store, run_id)
    delivery = DeliveryDecisionBuilder(store).build(
        run_id,
        review,
        recovery_reason="report_writer_smoke_review_recovery" if not review else None,
    )
    if delivery.delivery_mode == "fallback":
        raise ValueError(
            f"Run {run_id} does not contain three traceable candidate logics"
        )
    register_recovery_review(store, run_id, delivery, review)
    parameter_card_id = _parameter_card_id(store, run_id)
    context = ReportContextBuilder(store, max_chars=_REPORT_CONTEXT_MAX_CHARS).build(
        run_id,
        review,
        delivery_decision=delivery.model_dump(mode="json"),
    )
    context_path = write_report_context(root, run_id, context)
    gateway = OpenHarnessRuntimeGateway(
        InvestmentResearchRuntimeAdapter(project_root=root, evidence_store=store)
    )
    result = await gateway.execute(
        agent_id="report_writer",
        input_payload={
            "protocol_version": "1.0",
            "run_id": run_id,
            "task_id": "TASK-REPORT-WRITER-SMOKE-001",
            "objective": "基于紧凑报告材料包生成完整上市公司研究报告。",
            "agent_id": "report_writer",
            "parameter_card": {
                "parameter_card_id": parameter_card_id,
                "version": "1.0",
            },
            "review_id": delivery.review_id,
            "delivery_mode": delivery.delivery_mode,
            "selected_logic_ids": delivery.selected_logic_ids,
            "approved_fact_ids": review.get("approved_fact_ids", []),
            "approved_logic_ids": (
                delivery.selected_logic_ids if delivery.delivery_mode == "formal" else []
            ),
            "approved_catalyst_ids": review.get("approved_catalyst_ids", []),
            "approved_risk_ids": review.get("approved_risk_ids", []),
        },
        task_prompt=(
            "不得调用工具。只使用 context_package.report_context。输出轻量 ReportResult JSON。"
            "sections 必须恰好包含八个 section_id：company_overview、operating_changes、"
            "investment_logics、peer_comparison、catalysts、risks、tracking_indicators、"
            "limitations。included_logic_ids 必须恰好使用材料包中的三条 L-ID。核心章节"
            "必须填写 evidence_ids。不得新增材料包中不存在的事实或编号。"
            "每个章节正文不超过350个中文字符，全文保持简洁，避免重复材料包内容。"
        ),
        context_package={"report_context": context},
        max_turns=2,
        max_output_tokens=12_000,
        tool_call_limits={},
        timeout_seconds=300,
        retry_transient=True,
    )

    fallback_used = result.status != "succeeded"
    report_path: Path
    if result.status == "succeeded":
        report_path = write_report_markdown(
            root,
            run_id,
            result.structured_output or {},
            review_output=review,
            delivery_decision=delivery.model_dump(mode="json"),
        )
    else:
        report_path = run_output_dir(root, run_id) / "report.md"
        write_fallback_report(
            store=store,
            run_id=run_id,
            output_path=report_path,
            results={"report_writer": result},
            receipts=[
                {
                    "agent_id": "report_writer",
                    "execution_status": result.status,
                    "tool_count": len(result.tool_calls),
                    "failure_class": result.failure_class,
                    "error": result.error,
                }
            ],
            reason=f"ReportWriter smoke failed: {result.error or result.status}",
            review_output=review,
        )

    payload = {
        "mode": "ReportWriterSmoke",
        "run_id": run_id,
        "execution_status": result.status,
        "model": result.model,
        "usage": result.usage.model_dump(mode="json"),
        "tool_names": [item.tool_name for item in result.tool_calls],
        "tool_count": len(result.tool_calls),
        "schema_valid": result.status == "succeeded",
        "fallback_used": fallback_used,
        "formal_report_succeeded": result.status == "succeeded",
        "delivery_mode": delivery.delivery_mode,
        "delivery_decision": delivery.model_dump(mode="json"),
        "recovery_used": delivery.recovery_used,
        "context_path": str(context_path),
        "report_path": str(report_path),
        "warnings": result.warnings,
        "error": result.error,
        "pass": bool(
            result.status == "succeeded"
            and result.model == "deepseek-v4-flash"
            and result.usage.total_tokens > 0
            and not result.tool_calls
            and not fallback_used
        ),
    }
    summary_path = write_run_summary(
        root,
        run_id,
        payload,
        compatibility_name="report-writer-smoke.json",
    )
    return payload, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    payload, summary_path = asyncio.run(run(args.run_id))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"ReportWriter smoke receipt: {summary_path}")
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
