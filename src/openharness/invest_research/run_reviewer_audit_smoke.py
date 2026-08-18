"""Low-cost real Reviewer validation against an existing research Run."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from openharness.invest_research.evidence_audit import (
    EvidenceAuditService,
    review_id_for,
)
from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)


_UPSTREAM_ROLES = (
    "fundamental",
    "industry_competition",
    "market_catalyst",
    "risk",
)


async def run(run_id: str) -> dict[str, Any]:
    gateway = OpenHarnessRuntimeGateway()
    store = gateway.evidence_store
    if store is None or not store.run_exists(run_id):
        raise KeyError(f"Unknown run_id: {run_id}")

    audit = EvidenceAuditService(store).audit(
        run_id,
        "initial",
        audit_label="audit-smoke",
    )
    parameter_cards = store.query_records(
        run_id,
        record_types=("parameter_card",),
        limit=5,
    )
    if not parameter_cards:
        raise RuntimeError("ReviewerAuditSmoke requires an existing ParameterCard")
    parameter = parameter_cards[0]
    parameter_payload = dict(parameter.get("payload") or {})
    parameter_card_id = str(
        parameter_payload.get("parameter_card_id")
        or parameter.get("parameter_card_id")
        or ""
    )

    artifacts = store.query_records(
        run_id,
        record_types=("artifact",),
        submitted_by=_UPSTREAM_ROLES,
        limit=100,
    )
    artifact_ids = _record_ids(artifacts, "artifact_id")
    if not artifact_ids:
        raise RuntimeError("ReviewerAuditSmoke requires existing research artifacts")

    fact_ids = _unique(
        fact_id
        for item in audit.logic_checks
        for fact_id in item.valid_fact_ids
    )
    source_ids = _unique(
        source_id
        for item in audit.logic_checks
        for source_id in item.source_ids
    )
    expected_review_id = review_id_for(run_id, "audit-smoke")
    result = await gateway.execute(
        agent_id="reviewer_arbiter",
        input_payload={
            "protocol_version": "1.0",
            "run_id": run_id,
            "task_id": "TASK-REVIEWER-AUDIT-SMOKE-001",
            "objective": "抽查已有研究的 S/F/L 证据链，并按适中门槛给出审查结果。",
            "agent_id": "reviewer_arbiter",
            "review_stage": "initial",
            "expected_review_id": expected_review_id,
            "audit_artifact_id": audit.audit_id,
            "audit_status": audit.status,
            "parameter_card": {
                "parameter_card_id": parameter_card_id,
                "version": str(parameter_payload.get("version") or "1.0"),
            },
            "research_artifact_ids": artifact_ids,
            "source_ids": source_ids[:30],
            "fact_ids": fact_ids[:30],
            "logic_ids": audit.checked_logic_ids[:8],
        },
        task_prompt=(
            "先阅读 evidence_audit，再调用 evidence_query 一到两次定向抽查 L-ID 及其 F-ID、S-ID。"
            "若存在三条满足最低可追溯底线的逻辑，普通来源等级、单条事实支持或角色集中问题"
            "应 approve_with_warnings，不得阻断报告。禁止外部搜索，输出 ReviewDecision JSON。"
        ),
        context_package={
            "evidence_audit": audit.model_dump(mode="json"),
            "moderate_review_policy": True,
        },
        max_turns=4,
        tool_call_limits={"evidence_query": 2},
        timeout_seconds=120,
        retry_transient=False,
    )
    receipt = {
        "mode": "ReviewerAuditSmoke",
        "run_id": run_id,
        "audit": audit.model_dump(mode="json"),
        "execution_status": result.status,
        "model": result.model,
        "usage": result.usage.model_dump(mode="json"),
        "tool_names": [item.tool_name for item in result.tool_calls],
        "tool_count": len(result.tool_calls),
        "review_id": result.review_id,
        "structured_output": result.structured_output,
        "error": result.error,
        "pass": (
            result.status == "succeeded"
            and result.review_id == expected_review_id
            and 1 <= len(result.tool_calls) <= 2
            and all(item.tool_name == "evidence_query" for item in result.tool_calls)
        ),
    }
    _write_receipt(receipt)
    return receipt


def _write_receipt(receipt: dict[str, Any]) -> None:
    project_root = Path(__file__).resolve().parents[3]
    output_dir = project_root / ".openharness" / "validation"
    run_dir = output_dir / str(receipt["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2)
    (run_dir / "reviewer-audit-smoke.json").write_text(rendered, encoding="utf-8-sig")
    (output_dir / "reviewer-audit-smoke.json").write_text(rendered, encoding="utf-8-sig")


def _record_ids(records: list[dict[str, Any]], key: str) -> list[str]:
    return _unique(
        str((record.get("payload") or {}).get(key) or record.get(key) or "")
        for record in records
    )


def _unique(values) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in output:
            output.append(text)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    receipt = asyncio.run(run(args.run_id))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    raise SystemExit(0 if receipt["pass"] else 1)


if __name__ == "__main__":
    main()
