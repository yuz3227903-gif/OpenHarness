from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from openharness.invest_research.evidence_audit import EvidenceAuditService
from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.source_quality import classify_source_grade


def _store() -> tuple[Path, EvidenceStore, str]:
    root = (
        Path(__file__).resolve().parents[2]
        / ".openharness"
        / "data"
        / "tests"
        / f"evidence-audit-{uuid4().hex}"
    )
    root.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(root / "evidence.sqlite3", upload_root=root / "uploads")
    run_id = "RUN-EVIDENCE-AUDIT-001"
    store.create_run(run_id, "示例公司")
    return root, store, run_id


def _logic(
    store: EvidenceStore,
    run_id: str,
    index: int,
    *,
    role: str = "fundamental",
    grade: str = "C",
    source: bool = True,
) -> str:
    logic_id = f"L-AUDIT-{index:03d}"
    fact_id = f"F-AUDIT-{index:03d}"
    source_ids = []
    if source:
        source_ids.append(
            store.register_source(
                run_id,
                url_or_file=f"https://example.com/{index}",
                title=f"source-{index}",
                source_grade=grade,
                submitted_by=role,
            )
        )
    store.upsert_record(
        "fact",
        fact_id,
        run_id,
        {"fact_id": fact_id, "source_ids": source_ids},
        status="candidate",
        submitted_by=role,
    )
    store.upsert_record(
        "logic",
        logic_id,
        run_id,
        {"logic_id": logic_id, "supporting_fact_ids": [fact_id]},
        status="candidate",
        submitted_by=role,
    )
    return logic_id


def test_c_grade_and_single_role_are_warnings_not_failures():
    root, store, run_id = _store()
    try:
        logic_ids = [_logic(store, run_id, index) for index in range(1, 4)]

        result = EvidenceAuditService(store).audit(
            run_id, "initial", logic_ids=logic_ids
        )

        assert result.status == "pass_with_warnings"
        assert result.eligible_logic_ids == logic_ids
        assert any("one research Agent" in item for item in result.warnings)
        assert any("no A/B-grade source" in item for item in result.warnings)
        assert store.get_record(run_id, result.audit_id) is not None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_fact_source_breaks_minimum_traceability():
    root, store, run_id = _store()
    try:
        _logic(store, run_id, 1)
        _logic(store, run_id, 2)
        broken = _logic(store, run_id, 3, source=False)

        result = EvidenceAuditService(store).audit(run_id, "final")

        assert result.status == "fail"
        assert broken not in result.eligible_logic_ids
        assert len(result.eligible_logic_ids) == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_high_conflict_forces_audit_fail_but_keeps_traceable_logics():
    root, store, run_id = _store()
    try:
        for index, role in enumerate(
            ("fundamental", "industry_competition", "market_catalyst"), start=1
        ):
            _logic(store, run_id, index, role=role, grade="B")

        result = EvidenceAuditService(store).audit(
            run_id,
            "final",
            prior_review={
                "conflicts": [
                    {"conflict_id": "CONFLICT-001", "severity": "high"}
                ]
            },
        )

        assert result.status == "fail"
        assert len(result.eligible_logic_ids) == 3
        assert result.high_severity_conflicts == ["CONFLICT-001"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_known_official_domains_are_upgraded_conservatively():
    assert classify_source_grade("https://www.cninfo.com.cn/report", declared_grade="C") == "A"
    assert classify_source_grade("https://example.com/report", declared_grade="C") == "C"
