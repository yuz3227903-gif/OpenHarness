from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.report_context import (
    ReportContextBuilder,
    ReportContextError,
)


def _store() -> tuple[Path, EvidenceStore, str]:
    root = (
        Path(__file__).resolve().parents[2]
        / ".openharness"
        / "data"
        / "tests"
        / f"report-context-{uuid4().hex}"
    )
    root.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(root / "evidence.sqlite3", upload_root=root / "uploads")
    run_id = "RUN-REPORT-CONTEXT-001"
    store.create_run(run_id, "示例公司")
    store.upsert_record(
        "parameter_card",
        "PC-REPORT-001",
        run_id,
        {
            "parameter_card_id": "PC-REPORT-001",
            "company_identity": {
                "short_name": "示例公司",
                "ticker": "000001.SZ",
            },
            "research_period": {
                "start_date": "2025-08-12",
                "end_date": "2026-08-11",
            },
            "catalyst_window": {
                "start_date": "2026-08-12",
                "end_date": "2027-02-11",
            },
            "recommended_competitors": [
                {"company_name": "竞品甲"},
                {"company_name": "竞品乙"},
            ],
        },
        status="confirmed",
        submitted_by="planner",
    )
    for index in range(1, 4):
        fact_id = f"F-REPORT-00{index}"
        logic_id = f"L-REPORT-00{index}"
        store.upsert_record(
            "fact",
            fact_id,
            run_id,
            {
                "fact_id": fact_id,
                "metric_name": f"指标{index}",
                "value": index,
                "period": "FY2025",
                "source_ids": ["S-REPORT-001"],
            },
            status="verified",
            submitted_by="fundamental",
        )
        store.upsert_record(
            "logic",
            logic_id,
            run_id,
            {
                "logic_id": logic_id,
                "title": f"逻辑{index}",
                "mechanism": "演示机制",
                "supporting_fact_ids": [fact_id],
                "source_ids": ["S-REPORT-001"],
            },
            status="approved",
            submitted_by="fundamental",
        )
    store.register_source(
        run_id,
        url_or_file="https://example.com/report",
        title="示例公告",
        source_grade="A",
        status="verified",
        submitted_by="fundamental",
    )
    store.upsert_record(
        "logic",
        "L-UNAPPROVED-001",
        run_id,
        {
            "logic_id": "L-UNAPPROVED-001",
            "title": "不应进入材料包",
            "mechanism": "候选",
            "supporting_fact_ids": [],
        },
        status="candidate",
        submitted_by="fundamental",
    )
    return root, store, run_id


def _review(run_id: str) -> dict:
    del run_id
    return {
        "review_id": "REVIEW-REPORT-001",
        "decision": "approve_with_warnings",
        "approved_fact_ids": ["F-REPORT-001", "F-REPORT-002", "F-REPORT-003"],
        "approved_logic_ids": ["L-REPORT-001", "L-REPORT-002", "L-REPORT-003"],
        "approved_catalyst_ids": [],
        "approved_risk_ids": [],
        "issues": [
            {
                "issue_id": "ISSUE-REPORT-001",
                "severity": "medium",
                "problem_statement": "示例限制",
            }
        ],
        "limitations": ["仅用于离线测试"],
    }


def test_context_is_run_scoped_and_contains_three_approved_logics():
    root, store, run_id = _store()
    try:
        context = ReportContextBuilder(store).build(run_id, _review(run_id))

        assert context["run_id"] == run_id
        assert [item["logic_id"] for item in context["investment_logics"]] == [
            "L-REPORT-001",
            "L-REPORT-002",
            "L-REPORT-003",
        ]
        serialized = str(context)
        assert "L-UNAPPROVED-001" not in serialized
        assert "RUN-REPORT-CONTEXT-OTHER" not in serialized
        assert context["context_size_chars"] <= 48_000
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_rejects_unapproved_logic():
    root, store, run_id = _store()
    try:
        review = _review(run_id)
        review["approved_logic_ids"][2] = "L-UNAPPROVED-001"

        with pytest.raises(ReportContextError, match="approved logic records"):
            ReportContextBuilder(store).build(run_id, review)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_rejects_cross_run():
    root, store, run_id = _store()
    try:
        with pytest.raises(ReportContextError, match="unknown research Run"):
            ReportContextBuilder(store).build("RUN-REPORT-CONTEXT-OTHER", _review(run_id))
    finally:
        shutil.rmtree(root, ignore_errors=True)
